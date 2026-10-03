/**
 * Orb — Payments: the counter screen.
 *
 * Four surfaces, in the order a cashier's day runs:
 *
 *   COUNTER   open the till, take money, close the shift against a count
 *   RECEIPTS  what has been taken, and the only correction there is (reverse)
 *   APPROVALS refunds and discounts waiting on a second person's name
 *   DAY-END   the day's takings by method, with the total proved against its parts
 *
 * WHAT THIS SCREEN REFUSES TO DO
 * ──────────────────────────────
 * • It never shows an editable receipt. A receipt is a document, and the row
 *   says so ("Issued — cannot be edited"); the only action is Reverse, which
 *   writes an opposing entry the ledger keeps forever.
 * • It never renders an unknown amount as ₹0.00. An uncounted drawer reads
 *   "Not counted"; an undecided discount reads "Not decided". Every one of those
 *   comes from the server as `null` and goes through `formatInrOrUnset`.
 * • It never prints a receipt number as if it were official when the site has
 *   not configured its series — the banner says the numbers are placeholders.
 * • It computes NOTHING. Every figure on this screen was computed by
 *   services/money.py and arrives as both paise and a display string; lib/money
 *   is used only where the browser genuinely has to turn a typed rupee amount
 *   into paise before sending it.
 *
 * The amount field takes RUPEES because that is what a human types, and converts
 * with `toPaise` — which rejects floats and parses the string exactly. There is
 * no `parseFloat` anywhere in this file.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useFocusTrap } from '../../hooks/useFocusTrap'
import { getUser } from '../../api/client'
import { formatClinicalDate, formatClinicalDateKey, formatClinicalDateTime, localDateKey, parseUTC } from '../../lib/datetime'
import { assertOnlineForWrite, isOfflineWriteRefused } from '../../lib/offline/writeGuard'
import { formatInr, formatInrOrUnset, toPaise, MoneyError } from '../../lib/money'
import {
  COUNTER_METHODS,
  closeCounter,
  decideDiscount,
  decideRefund,
  getCounterExpected,
  getDayEnd,
  listCounters,
  listDiscounts,
  listOpenInvoices,
  listReceipts,
  listRefunds,
  mrnText,
  needsReference,
  newIdempotencyKey,
  openCounter,
  payRefund,
  recordPayment,
  reverseReceipt,
  type CounterExpected,
  type CounterPatient,
  type CounterSession,
  type DayEnd,
  type DiscountApproval,
  type OpenInvoice,
  type PatientNames,
  type PaymentMethod,
  type Receipt,
  type Refund,
} from '../../api/payments'
import CounterPatientPicker from '../../components/payments/CounterPatientPicker'
import { humaniseEnum, plural } from '../../lib/humanise'
import './PaymentsPanel.css'

type TabId = 'counter' | 'receipts' | 'approvals' | 'dayend'

const TABS: { id: TabId; label: string }[] = [
  { id: 'counter', label: 'Counter' },
  { id: 'receipts', label: 'Receipts' },
  { id: 'approvals', label: 'Approvals' },
  { id: 'dayend', label: 'Day end' },
]

const METHOD_LABEL: Record<string, string> = {
  cash: 'Cash', card: 'Card', upi: 'UPI', netbanking: 'Net banking',
  cheque: 'Cheque', dd: 'Demand draft', neft: 'NEFT', rtgs: 'RTGS',
  wallet: 'Wallet', insurance: 'Insurance', scheme: 'Scheme',
  adjustment: 'Adjustment',
}

function describeError(error: unknown): string {
  if (isOfflineWriteRefused(error)) return error.message
  if (error instanceof MoneyError) return sentence(error.message)
  if (error instanceof Error) return sentence(error.message)
  return 'Something went wrong'
}

// The service writes its refusals in lower case ("counter session 3 is
// closed; ..."); a banner on the counter starts with a capital.
function sentence(text: string): string {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text
}

/** Who a money row is for, as a clerk reads it: name and MRN. The server
 *  leaves the name null when the id names nobody, or names a patient outside
 *  this account's ward, and then only the id it already showed is printed. */
function PatientLabel({ row }: { row: { patient_id: string } & PatientNames }) {
  if (!row.patient_name) return <>ID {row.patient_id}</>
  return (
    <>
      {row.patient_name}
      {row.patient_mrn && <span className="pay-list__sub"> · {mrnText(row.patient_mrn)}</span>}
    </>
  )
}

/** A member of staff by name; the stored id only when no user has it. */
function staffName(name: string | null | undefined, id: string | null | undefined): string {
  return name || id || 'unknown'
}

/** Rupees typed by a human → integer paise, or a message naming the problem.
 *  `toPaise` never uses parseFloat and rejects anything finer than a paise. */
function rupeesToPaise(text: string): { paise: number } | { error: string } {
  const trimmed = text.trim()
  if (!trimmed) return { error: 'Enter an amount' }
  try {
    const paise = toPaise(trimmed)
    if (paise <= 0) return { error: 'Amount must be more than zero' }
    return { paise }
  } catch (e) {
    return { error: e instanceof Error ? e.message : 'Enter a valid amount' }
  }
}

/* ── Reverse-receipt modal ───────────────────────────────────────────────── */

function ReverseModal({
  receipt, onClose, onDone,
}: {
  receipt: Receipt
  onClose: () => void
  onDone: (message: string) => void
}) {
  // Returns the ref AND owns focus restoration + Escape — see hooks/useFocusTrap.
  const ref = useFocusTrap<HTMLDivElement>(onClose)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    if (!reason.trim()) {
      setError('A reversal needs a reason. It stays on the ledger next to the receipt.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      assertOnlineForWrite('reverse a receipt')
      const result = await reverseReceipt(receipt.id, reason.trim())
      onDone(
        `Reversed ${receipt.receipt_number ?? `receipt #${receipt.id}`}. ` +
        `Reversal ${result.reversal.receipt_number ?? `#${result.reversal.id}`} issued.`,
      )
    } catch (e) {
      setError(describeError(e))
      setBusy(false)
    }
  }

  return createPortal(
    <div className="pay-modal__backdrop" onClick={onClose}>
      <div
        className="pay-modal"
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby="pay-reverse-title"
        onClick={e => e.stopPropagation()}
      >
        <h2 className="pay-modal__title" id="pay-reverse-title">Reverse this receipt</h2>
        <p className="pay-modal__body">
          A receipt is never edited. Reversing writes a new, opposing entry and keeps
          both on the ledger: {receipt.amount_display} by {METHOD_LABEL[receipt.method] ?? receipt.method},
          receipt {receipt.receipt_number ?? `#${receipt.id}`}.
        </p>
        <p className="pay-note">
          A second person signs for a reversal: whoever took the payment cannot reverse it.
        </p>
        <label className="pay-field">
          <span className="pay-field__label">Reason</span>
          <textarea
            className="pay-field__input pay-field__input--area"
            value={reason}
            onChange={e => setReason(e.target.value)}
            placeholder="Why is this being reversed?"
            rows={3}
            autoFocus
          />
        </label>
        {error && <p className="pay-alert pay-alert--danger" role="alert">{error}</p>}
        <div className="pay-modal__actions">
          <button type="button" className="pay-btn" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="pay-btn pay-btn--danger"
            onClick={submit}
            disabled={busy}
          >
            {busy ? 'Reversing…' : 'Reverse receipt'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

/* ── Counter: take money, open and close the till ────────────────────────── */

function CounterTab({
  onRecorded, notify,
}: {
  onRecorded: () => void
  notify: (message: string, tone?: 'ok' | 'danger') => void
}) {
  const [sessions, setSessions] = useState<CounterSession[]>([])
  const [expected, setExpected] = useState<CounterExpected | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [counterCode, setCounterCode] = useState('')
  const [floatRupees, setFloatRupees] = useState('')

  // The patient is CHOSEN from the register, never typed: a typed id was
  // receipted to whatever was typed, including nobody at all.
  const [patient, setPatient] = useState<CounterPatient | null>(null)
  const patientId = patient?.id ?? ''
  const [amount, setAmount] = useState('')
  const [method, setMethod] = useState<PaymentMethod>('cash')
  const [reference, setReference] = useState('')
  const [invoiceId, setInvoiceId] = useState('')
  const [formError, setFormError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [lastReceipt, setLastReceipt] = useState<Receipt | null>(null)
  // One key per FORM, regenerated after a successful receipt. A double-tapped
  // "Take payment" replays the same key and the server returns the SAME receipt
  // instead of taking the money twice.
  const idempotencyKey = useRef(newIdempotencyKey())

  // THE PATIENT'S OPEN BILLS. A typed invoice number was one wrong digit from
  // another patient's bill, and it was the internal id the screen never
  // showed. The choice now comes from this patient's own bills that still owe
  // money (number and balance). The answer remembers whose bills it holds, so
  // a slow reply for the previous patient never fills this one's list, and it
  // reloads after each payment because the balances moved.
  const [openInvoices, setOpenInvoices] = useState<
    { pid: string; invoices?: OpenInvoice[]; error?: string } | null
  >(null)
  const [invoicesToken, setInvoicesToken] = useState(0)
  useEffect(() => {
    if (!patientId) return
    let cancelled = false
    listOpenInvoices(patientId)
      .then(invoices => { if (!cancelled) setOpenInvoices({ pid: patientId, invoices }) })
      .catch(e => { if (!cancelled) setOpenInvoices({ pid: patientId, error: describeError(e) }) })
    return () => { cancelled = true }
  }, [patientId, invoicesToken])
  const invoicesFor = patientId && openInvoices?.pid === patientId ? openInvoices : null
  const invoiceChoices = invoicesFor?.invoices ?? []
  const chosenInvoice = invoiceChoices.find(i => String(i.invoice_id) === invoiceId) ?? null
  // Only an invoice from this patient's list can be chosen, so the one wait
  // is for the list itself.
  const invoiceReady = !invoiceId || chosenInvoice != null

  const choosePatient = (next: CounterPatient | null) => {
    setPatient(next)
    setInvoiceId('')
    setFormError(null)
    // A different patient is a different payment, never a replay of the last.
    idempotencyKey.current = newIdempotencyKey()
  }

  const [declared, setDeclared] = useState('')

  // WHICH DRAWER. Several counters can be open at once (OPD and IPD, or two
  // cashiers), and taking the first open one put this cashier's cash into
  // somebody else's drawer: theirs counted over, this one short. One open
  // counter is used as before; with several, the one this account opened is
  // the default, and otherwise the cashier picks before any money is taken.
  const [chosenSessionId, setChosenSessionId] = useState<number | null>(null)
  const openSessions = sessions.filter(s => s.status === 'open')
  const myId = getUser()?.id ?? null
  const openSession =
    openSessions.find(s => s.id === chosenSessionId)
    ?? (openSessions.length === 1
      ? openSessions[0]
      : openSessions.find(s => myId != null && s.opened_by === myId))
    ?? null
  const mustChooseCounter = openSessions.length > 1 && openSession == null
  // A drawer left open overnight is still taking money and has not been
  // counted since the day it was opened. Compared by local calendar day
  // (localDateKey), never by the UTC date, which is yesterday until 05:30 IST.
  const openedBeforeToday = !!openSession?.opened_at
    && localDateKey(parseUTC(openSession.opened_at)) < localDateKey()

  /* `loading` starts true and only ever falls. A refresh after a write must NOT
     blank the form the clerk is standing in front of, and setting state
     synchronously inside an effect body triggers a cascading render (see the
     react-hooks/set-state-in-effect rule). The write paths carry their own
     `busy` flag, which is the spinner a user actually needs.

     Open counters are fetched on their own: a till opened days ago drops out
     of the 20 most recent shifts, and a till this screen cannot see is a till
     nobody closes (the server then refuses to open that counter again). */
  const refresh = useCallback(async () => {
    try {
      const [recent, open] = await Promise.all([
        listCounters({ limit: 20 }),
        listCounters({ status: 'open', limit: 50 }),
      ])
      const seen = new Set(recent.map(s => s.id))
      setSessions([...recent, ...open.filter(s => !seen.has(s.id))])
      setError(null)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setLoading(false)
    }
  }, [])

  // The drawer's figures follow the drawer in use, and reload after every
  // refresh (a new `sessions` array) so a write shows up in Expected at once.
  const openSessionId = openSession?.id ?? null
  useEffect(() => {
    if (openSessionId == null) return
    let cancelled = false
    getCounterExpected(openSessionId)
      .then(report => { if (!cancelled) setExpected(report) })
      .catch(e => { if (!cancelled) setError(describeError(e)) })
    return () => { cancelled = true }
  }, [openSessionId, sessions])
  // Never another drawer's figures under this drawer's name.
  const drawer = expected && expected.session?.id === openSessionId ? expected : null

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void refresh() }, [refresh])

  const doOpen = async () => {
    setFormError(null)
    if (!counterCode.trim()) { setFormError('Give the counter a code'); return }
    let openingPaise = 0
    if (floatRupees.trim()) {
      const parsed = rupeesToPaise(floatRupees)
      if ('error' in parsed) { setFormError(parsed.error); return }
      openingPaise = parsed.paise
    }
    setBusy(true)
    try {
      assertOnlineForWrite('open a counter')
      const opened = await openCounter({
        counterCode: counterCode.trim().toUpperCase(), openingFloatPaise: openingPaise,
      })
      // The drawer you just opened is the one you are standing at.
      setChosenSessionId(opened.id)
      setCounterCode(''); setFloatRupees('')
      notify('Counter opened.')
      await refresh()
    } catch (e) {
      setFormError(describeError(e))
    } finally {
      setBusy(false)
    }
  }

  const doTake = async () => {
    setFormError(null)
    if (!patientId) { setFormError('Which patient is this for? Search by name, MRN or bed.'); return }
    if (!invoiceReady) {
      setFormError("Choose one of this patient's open bills, or none to hold the money as an advance.")
      return
    }
    if (mustChooseCounter) {
      setFormError('More than one counter is open. Choose your counter under The till first.')
      return
    }
    const parsed = rupeesToPaise(amount)
    if ('error' in parsed) { setFormError(parsed.error); return }
    if (needsReference(method) && !reference.trim()) {
      setFormError(
        `A ${METHOD_LABEL[method] ?? method} payment needs a reference number, without one ` +
        'it cannot be matched to the bank statement.',
      )
      return
    }
    setBusy(true)
    try {
      assertOnlineForWrite('record a payment')
      const receipt = await recordPayment({
        patientId,
        amountPaise: parsed.paise,
        method,
        purpose: chosenInvoice ? 'invoice_payment' : 'deposit',
        invoiceId: chosenInvoice ? chosenInvoice.invoice_id : null,
        referenceNo: reference.trim() || null,
        counterSessionId: openSession?.id ?? null,
        idempotencyKey: idempotencyKey.current,
      })
      setLastReceipt(receipt)
      idempotencyKey.current = newIdempotencyKey()
      setAmount(''); setReference(''); setInvoiceId('')
      setInvoicesToken(t => t + 1)
      notify(
        receipt.replayed
          ? 'That payment had already been taken, showing the original receipt.'
          : `Receipt ${receipt.receipt_number ?? `#${receipt.id}`} issued.`,
      )
      onRecorded()
      await refresh()
    } catch (e) {
      setFormError(describeError(e))
    } finally {
      setBusy(false)
    }
  }

  const doClose = async () => {
    if (!openSession) return
    setFormError(null)
    const parsed = rupeesToPaise(declared)
    if ('error' in parsed && declared.trim() !== '0') { setFormError(parsed.error); return }
    const declaredPaise = 'paise' in parsed ? parsed.paise : 0
    setBusy(true)
    try {
      assertOnlineForWrite('close a counter')
      const closed = await closeCounter(openSession.id, declaredPaise)
      setDeclared('')
      notify(
        closed.balanced
          ? 'Shift closed and balanced.'
          : `Shift closed with a variance of ${closed.variance_display}. It is recorded, not written off.`,
        closed.balanced ? 'ok' : 'danger',
      )
      await refresh()
    } catch (e) {
      setFormError(describeError(e))
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <p className="pay-muted">Loading counters…</p>
  if (error) return <p className="pay-alert pay-alert--danger" role="alert">{error}</p>

  return (
    <div className="pay-grid">
      <section className="pay-card" aria-labelledby="pay-take-title">
        <h2 className="pay-card__title" id="pay-take-title">Take a payment</h2>
        {mustChooseCounter ? (
          <p className="pay-alert pay-alert--warn">
            More than one counter is open. Choose your counter under The till before taking
            a payment, so the money is counted in the right drawer.
          </p>
        ) : !openSession && (
          <p className="pay-alert pay-alert--warn">
            No counter is open. A payment can still be recorded, but it will not appear on
            any shift's cash count.
          </p>
        )}
        <div className="pay-form">
          <CounterPatientPicker value={patient} onChange={choosePatient} />
          <label className="pay-field">
            <span className="pay-field__label">Amount (₹)</span>
            <input
              className="pay-field__input pay-num"
              value={amount}
              onChange={e => setAmount(e.target.value)}
              inputMode="decimal"
              placeholder="1500.00"
            />
          </label>
          <label className="pay-field">
            <span className="pay-field__label">Method</span>
            <select
              className="pay-field__input"
              value={method}
              onChange={e => setMethod(e.target.value as PaymentMethod)}
            >
              {COUNTER_METHODS.map(m => (
                <option key={m} value={m}>{METHOD_LABEL[m] ?? m}</option>
              ))}
            </select>
          </label>
          <label className="pay-field">
            <span className="pay-field__label">
              Reference{needsReference(method) ? ' (required)' : ''}
            </span>
            <input
              className="pay-field__input"
              value={reference}
              onChange={e => setReference(e.target.value)}
              placeholder={needsReference(method) ? 'UTR / auth code / cheque no.' : 'Optional'}
            />
          </label>
          <label className="pay-field">
            <span className="pay-field__label">Bill</span>
            <select
              className="pay-field__input"
              value={invoiceId}
              onChange={e => setInvoiceId(e.target.value)}
            >
              <option value="">None, hold as an advance</option>
              {invoiceChoices.map(inv => (
                <option key={inv.invoice_id} value={String(inv.invoice_id)}>
                  {inv.invoice_number ?? 'Unnumbered bill'} · {inv.due_display} due
                  {inv.issued_at ? ` · issued ${formatClinicalDate(inv.issued_at)}` : ''}
                </option>
              ))}
            </select>
          </label>
        </div>
        {patient && (
          invoicesFor?.error ? (
            <p className="pay-alert pay-alert--danger" role="alert">
              This patient&apos;s bills could not be loaded ({invoicesFor.error}). Money taken
              now is held as an advance.
            </p>
          ) : !invoicesFor ? (
            <p className="pay-muted">Loading this patient&apos;s open bills…</p>
          ) : chosenInvoice ? (
            <p className="pay-alert pay-alert--info" data-testid="invoice-choice">
              Bill {chosenInvoice.invoice_number ?? 'without a number'} for {patient.name}:
              {' '}{chosenInvoice.due_display} due of {chosenInvoice.payable_display}.
            </p>
          ) : invoiceChoices.length === 0 ? (
            <p className="pay-muted">
              {patient.name} has no open bills. Money taken now is held as an advance.
            </p>
          ) : null
        )}
        {formError && <p className="pay-alert pay-alert--danger" role="alert">{formError}</p>}
        <button
          type="button"
          className="pay-btn pay-btn--primary"
          onClick={doTake}
          disabled={busy || !invoiceReady || mustChooseCounter}
        >
          {busy ? 'Working…' : 'Take payment'}
        </button>

        {lastReceipt && (
          <div className="pay-receipt-slip">
            <p className="pay-receipt-slip__no">
              {lastReceipt.receipt_number ?? `Receipt #${lastReceipt.id}`}
            </p>
            <p className="pay-receipt-slip__amount pay-num">{lastReceipt.amount_display}</p>
            {lastReceipt.over_payment && (
              <p className="pay-alert pay-alert--info">
                {lastReceipt.allocated_display} went to the invoice.{' '}
                {lastReceipt.advance_display} is held as an advance for this patient.
                It is not a credit on the bill.
              </p>
            )}
            {lastReceipt.receipt_series_is_configured === false && (
              <p className="pay-alert pay-alert--warn">
                This receipt series has not been configured for this site, so the number
                above is a placeholder, not a GST receipt number.
              </p>
            )}
          </div>
        )}
      </section>

      <section className="pay-card" aria-labelledby="pay-till-title">
        <h2 className="pay-card__title" id="pay-till-title">The till</h2>
        {openSessions.length > 1 && (
          <label className="pay-field">
            <span className="pay-field__label">Your counter</span>
            <select
              className="pay-field__input"
              value={openSession?.id ?? ''}
              onChange={e => setChosenSessionId(e.target.value ? Number(e.target.value) : null)}
            >
              {!openSession && <option value="">Choose the counter you are working at</option>}
              {openSessions.map(s => (
                <option key={s.id} value={s.id}>
                  {s.counter_code}, opened by {staffName(s.opened_by_name, s.opened_by)}
                </option>
              ))}
            </select>
          </label>
        )}
        {openSession ? (
          <>
            {openedBeforeToday && (
              <p className="pay-alert pay-alert--warn" data-testid="till-opened-earlier">
                This counter has been open since {formatClinicalDate(openSession.opened_at)} and
                has not been counted since. Count the drawer and close that shift, then open
                today's.
              </p>
            )}
            <dl className="pay-facts">
              <div className="pay-fact">
                <dt>Counter</dt>
                <dd>{openSession.counter_code}</dd>
              </div>
              <div className="pay-fact">
                <dt>Opened</dt>
                <dd>
                  {openSession.opened_at ? formatClinicalDateTime(openSession.opened_at) : 'Not recorded'}
                  {' by '}{staffName(openSession.opened_by_name, openSession.opened_by)}
                </dd>
              </div>
              <div className="pay-fact">
                <dt>Opening float</dt>
                <dd className="pay-num">{openSession.opening_float_display}</dd>
              </div>
              <div className="pay-fact">
                <dt>Cash in</dt>
                <dd className="pay-num">{drawer?.cash_in_display ?? '—'}</dd>
              </div>
              <div className="pay-fact">
                <dt>Cash out</dt>
                <dd className="pay-num">{drawer?.cash_out_display ?? '—'}</dd>
              </div>
              <div className="pay-fact pay-fact--strong">
                <dt>Expected in drawer</dt>
                <dd className="pay-num">{drawer?.expected_cash_display ?? '—'}</dd>
              </div>
            </dl>
            <label className="pay-field">
              <span className="pay-field__label">Counted cash (₹)</span>
              <input
                className="pay-field__input pay-num"
                value={declared}
                onChange={e => setDeclared(e.target.value)}
                inputMode="decimal"
                placeholder="What is actually in the drawer"
              />
            </label>
            <p className="pay-note">
              A difference is recorded as a variance and the shift closes as disputed. It is
              never quietly absorbed.
            </p>
            <button type="button" className="pay-btn" onClick={doClose} disabled={busy}>
              Close shift
            </button>
          </>
        ) : (
          <>
            <p className="pay-muted">
              {mustChooseCounter
                ? 'Choose your counter above, or open a new one.'
                : 'No open counter.'}
            </p>
            <div className="pay-form">
              <label className="pay-field">
                <span className="pay-field__label">Counter code</span>
                <input
                  className="pay-field__input"
                  value={counterCode}
                  onChange={e => setCounterCode(e.target.value)}
                  placeholder="OPD-1"
                />
              </label>
              <label className="pay-field">
                <span className="pay-field__label">Opening float (₹)</span>
                <input
                  className="pay-field__input pay-num"
                  value={floatRupees}
                  onChange={e => setFloatRupees(e.target.value)}
                  inputMode="decimal"
                  placeholder="0.00"
                />
              </label>
            </div>
            <button type="button" className="pay-btn pay-btn--primary" onClick={doOpen} disabled={busy}>
              Open counter
            </button>
          </>
        )}

        {sessions.filter(s => s.status !== 'open').length > 0 && (
          <div className="pay-subsection">
            <h3 className="pay-subsection__title">Recent shifts</h3>
            <ul className="pay-list">
              {sessions.filter(s => s.status !== 'open').slice(0, 6).map(s => (
                <li key={s.id} className="pay-list__row">
                  <span className="pay-list__main">{s.counter_code}</span>
                  <span className={`pay-chip pay-chip--${s.status === 'disputed' ? 'danger' : 'ok'}`}>
                    {humaniseEnum(s.status)}
                  </span>
                  <span className="pay-num pay-list__amount">{s.variance_display}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>
    </div>
  )
}

/* ── Receipts ────────────────────────────────────────────────────────────── */

function ReceiptsTab({
  reloadToken, notify,
}: {
  reloadToken: number
  notify: (message: string, tone?: 'ok' | 'danger') => void
}) {
  const [receipts, setReceipts] = useState<Receipt[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reversing, setReversing] = useState<Receipt | null>(null)

  const load = useCallback(async () => {
    try {
      setReceipts(await listReceipts({ limit: 50 }))
      setError(null)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setLoading(false)
    }
  }, [])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void load() }, [load, reloadToken])

  if (loading) return <p className="pay-muted">Loading receipts…</p>
  if (error) return <p className="pay-alert pay-alert--danger" role="alert">{error}</p>
  if (receipts.length === 0) return <p className="pay-muted">No receipts yet.</p>

  return (
    <>
      <div className="pay-tablewrap">
        <table className="pay-table">
          <caption className="pay-table__caption">
            Every receipt here is issued and cannot be edited. A mistake is corrected by
            reversing it, which writes an opposing entry and keeps both.
          </caption>
          <thead>
            <tr>
              <th scope="col">Receipt</th>
              <th scope="col">Received</th>
              <th scope="col">Patient</th>
              <th scope="col">Method</th>
              <th scope="col" className="pay-th--num">Amount</th>
              <th scope="col">Taken by</th>
              <th scope="col">State</th>
              <th scope="col"><span className="pay-visually-hidden">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {receipts.map(r => (
              <tr key={r.id} className={r.is_reversed ? 'pay-row--reversed' : undefined}>
                <td>{r.receipt_number ?? `#${r.id}`}</td>
                {/* Local (IST) time via parseUTC: received_at is stored UTC. */}
                <td>{r.received_at ? formatClinicalDateTime(r.received_at) : 'Not recorded'}</td>
                <td><PatientLabel row={r} /></td>
                <td>{METHOD_LABEL[r.method] ?? r.method}</td>
                <td className="pay-num pay-td--num">
                  {/* The same hyphen-minus formatInr writes everywhere else
                      ("-₹100.00"); this one cell used U+2212. */}
                  {r.direction === 'out' ? `-${r.amount_display}` : r.amount_display}
                </td>
                <td>{staffName(r.received_by_name, r.received_by)}</td>
                <td>
                  {r.is_reversed ? (
                    <span className="pay-chip pay-chip--danger">Reversed</span>
                  ) : r.direction === 'out' ? (
                    <span className="pay-chip pay-chip--info">Paid out</span>
                  ) : (
                    <span className="pay-chip pay-chip--ok">Issued: cannot be edited</span>
                  )}
                </td>
                <td>
                  {r.direction === 'in' && !r.is_reversed && (
                    <button
                      type="button"
                      className="pay-btn pay-btn--small"
                      onClick={() => setReversing(r)}
                    >
                      Reverse
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {reversing && (
        <ReverseModal
          receipt={reversing}
          onClose={() => setReversing(null)}
          onDone={message => {
            setReversing(null)
            notify(message)
            void load()
          }}
        />
      )}
    </>
  )
}

/* ── Approvals: refunds and discounts ────────────────────────────────────── */

function ApprovalsTab({
  reloadToken, notify,
}: {
  reloadToken: number
  notify: (message: string, tone?: 'ok' | 'danger') => void
}) {
  const [refunds, setRefunds] = useState<Refund[]>([])
  const [discounts, setDiscounts] = useState<DiscountApproval[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [r, d] = await Promise.all([
        listRefunds({ limit: 50 }),
        listDiscounts({ limit: 50 }),
      ])
      setRefunds(r)
      setDiscounts(d)
      setError(null)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setLoading(false)
    }
  }, [])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void load() }, [load, reloadToken])

  const act = async (key: string, fn: () => Promise<unknown>, done: string) => {
    setBusyId(key)
    try {
      assertOnlineForWrite('record a decision')
      await fn()
      notify(done)
      await load()
    } catch (e) {
      notify(describeError(e), 'danger')
    } finally {
      setBusyId(null)
    }
  }

  if (loading) return <p className="pay-muted">Loading approvals…</p>
  if (error) return <p className="pay-alert pay-alert--danger" role="alert">{error}</p>

  return (
    <div className="pay-grid">
      <section className="pay-card" aria-labelledby="pay-refunds-title">
        <h2 className="pay-card__title" id="pay-refunds-title">Refunds</h2>
        <p className="pay-note">
          A refund needs a second person: whoever raised it cannot approve it.
        </p>
        {refunds.length === 0 ? <p className="pay-muted">Nothing to show.</p> : (
          <ul className="pay-list">
            {refunds.map(r => (
              <li key={r.id} className="pay-list__row pay-list__row--stacked">
                <div className="pay-list__head">
                  <span className="pay-list__main">{r.refund_number ?? `#${r.id}`}</span>
                  <span className="pay-num pay-list__amount">{r.amount_display}</span>
                </div>
                <p className="pay-list__sub">
                  <PatientLabel row={r} /> · {r.reason}
                </p>
                <div className="pay-list__foot">
                  <span className={`pay-chip pay-chip--${
                    r.status === 'paid' ? 'ok'
                      : r.status === 'rejected' ? 'danger'
                        : r.status === 'approved' ? 'info' : 'warn'
                  }`}>{humaniseEnum(r.status)}</span>
                  <span className="pay-list__sub">
                    Raised by {staffName(r.requested_by_name, r.requested_by)}
                    {r.requested_at ? `, ${formatClinicalDateTime(r.requested_at)}` : ''}
                    {r.decided_by ? ` · decided by ${staffName(r.decided_by_name, r.decided_by)}` : ''}
                  </span>
                  {r.status === 'requested' && (
                    <>
                      <button
                        type="button"
                        className="pay-btn pay-btn--small"
                        disabled={busyId === `r${r.id}`}
                        onClick={() => act(`r${r.id}`, () => decideRefund(r.id, true), 'Refund approved.')}
                      >
                        Approve
                      </button>
                      <button
                        type="button"
                        className="pay-btn pay-btn--small"
                        disabled={busyId === `r${r.id}`}
                        onClick={() => act(`r${r.id}`, () => decideRefund(r.id, false), 'Refund rejected.')}
                      >
                        Reject
                      </button>
                    </>
                  )}
                  {r.status === 'approved' && (
                    <button
                      type="button"
                      className="pay-btn pay-btn--small pay-btn--primary"
                      disabled={busyId === `r${r.id}`}
                      onClick={() => act(
                        `r${r.id}`,
                        () => payRefund({ refundId: r.id, method: 'cash' }),
                        'Refund paid out in cash.',
                      )}
                    >
                      Pay in cash
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="pay-card" aria-labelledby="pay-discounts-title">
        <h2 className="pay-card__title" id="pay-discounts-title">Discounts</h2>
        <p className="pay-note">
          A discount is a person's decision. Both names are kept: who asked, and who agreed.
        </p>
        {discounts.length === 0 ? <p className="pay-muted">Nothing to show.</p> : (
          <ul className="pay-list">
            {discounts.map(d => (
              <li key={d.id} className="pay-list__row pay-list__row--stacked">
                <div className="pay-list__head">
                  <span className="pay-list__main"><PatientLabel row={d} /></span>
                  <span className="pay-num pay-list__amount">{d.requested_display}</span>
                </div>
                <p className="pay-list__sub">{d.reason}</p>
                <div className="pay-list__foot">
                  <span className={`pay-chip pay-chip--${
                    d.status === 'approved' ? 'ok' : d.status === 'rejected' ? 'danger' : 'warn'
                  }`}>{humaniseEnum(d.status)}</span>
                  <span className="pay-list__sub">
                    Asked by {staffName(d.requested_by_name, d.requested_by)}
                    {d.decided_by ? `, decided by ${staffName(d.decided_by_name, d.decided_by)}` : ''}
                    {' · '}approved{' '}
                    <span className="pay-num">{d.approved_display}</span>
                  </span>
                  {d.exceeded_limit && (
                    <span className="pay-chip pay-chip--warn">
                      Over the policy limit of {d.policy_limit_display}
                    </span>
                  )}
                  {d.status === 'requested' && (
                    <>
                      <button
                        type="button"
                        className="pay-btn pay-btn--small"
                        disabled={busyId === `d${d.id}`}
                        onClick={() => act(`d${d.id}`, () => decideDiscount(d.id, true), 'Discount approved.')}
                      >
                        Approve
                      </button>
                      <button
                        type="button"
                        className="pay-btn pay-btn--small"
                        disabled={busyId === `d${d.id}`}
                        onClick={() => act(`d${d.id}`, () => decideDiscount(d.id, false), 'Discount rejected.')}
                      >
                        Reject
                      </button>
                    </>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

/* ── Day end ─────────────────────────────────────────────────────────────── */

function DayEndTab({ reloadToken }: { reloadToken: number }) {
  const [day, setDay] = useState<DayEnd | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    // No synchronous setState here: the first paint already shows the loader,
    // and a refetch replaces the sheet in place rather than flashing it away.
    getDayEnd()
      .then(d => { if (!cancelled) { setDay(d); setError(null) } })
      .catch(e => { if (!cancelled) setError(describeError(e)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [reloadToken])

  // The screen re-adds the breakdown it was given. If the server's headline ever
  // disagreed with its own rows, the clerk would see it here rather than trust
  // the big number — the server also refuses to serve a sheet that does not
  // reconcile, so this should be unreachable.
  const partsSum = useMemo(
    () => (day ? day.by_method.reduce((total, row) => total + row.in_paise, 0) : 0),
    [day],
  )
  const disagrees = day != null && partsSum !== day.totals.in_paise

  if (loading) return <p className="pay-muted">Loading the day's takings…</p>
  if (error) return <p className="pay-alert pay-alert--danger" role="alert">{error}</p>
  if (!day) return null

  return (
    <section className="pay-card" aria-labelledby="pay-dayend-title">
      <h2 className="pay-card__title" id="pay-dayend-title">
        Day end · {formatClinicalDateKey(day.day)}
      </h2>

      {disagrees && (
        <p className="pay-alert pay-alert--danger" role="alert">
          This sheet does not add up ({formatInr(partsSum)} across the methods against a
          stated total of {day.totals.in_display}). Do not sign it.
        </p>
      )}

      {/* The server says WHY in words: an open drawer, a drawer left open since
          an earlier day (named, with who opened it), or cash in no drawer. A
          bare count read "0 counters still open" on a sheet held open by
          drawer-less cash. */}
      {!day.is_final && (
        day.finality_blockers && day.finality_blockers.length > 0 ? (
          <div className="pay-alert pay-alert--warn" data-testid="dayend-blockers">
            The day is not counted yet.
            <ul style={{ margin: 'var(--space-2) 0 0', paddingLeft: 'var(--space-5)' }}>
              {day.finality_blockers.map(reason => <li key={reason}>{reason}.</li>)}
            </ul>
          </div>
        ) : (
          <p className="pay-alert pay-alert--warn">
            {day.open_session_count} counter{day.open_session_count === 1 ? '' : 's'} still open.
            The day is not counted yet.
          </p>
        )
      )}

      {!day.receipt_series_is_configured && (
        <p className="pay-alert pay-alert--warn">
          The receipt series is not configured for this site, so today's receipt numbers are
          placeholders.
        </p>
      )}

      <div className="pay-tablewrap">
        <table className="pay-table">
          <thead>
            <tr>
              <th scope="col">Method</th>
              <th scope="col" className="pay-th--num">Taken</th>
              <th scope="col" className="pay-th--num">Paid out</th>
              <th scope="col" className="pay-th--num">Net</th>
              <th scope="col" className="pay-th--num">Receipts</th>
            </tr>
          </thead>
          <tbody>
            {day.by_method.length === 0 ? (
              <tr><td colSpan={5} className="pay-muted">Nothing taken today.</td></tr>
            ) : day.by_method.map(row => (
              <tr key={row.method}>
                <td>{METHOD_LABEL[row.method] ?? row.method}</td>
                <td className="pay-num pay-td--num">{row.in_display}</td>
                <td className="pay-num pay-td--num">{row.out_display}</td>
                <td className="pay-num pay-td--num">{row.net_display}</td>
                <td className="pay-num pay-td--num">{row.in_count + row.out_count}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th scope="row">Total</th>
              <td className="pay-num pay-td--num">{day.totals.in_display}</td>
              <td className="pay-num pay-td--num">{day.totals.out_display}</td>
              <td className="pay-num pay-td--num">{day.totals.net_display}</td>
              <td className="pay-num pay-td--num">
                {day.totals.in_count + day.totals.out_count}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>

      <dl className="pay-facts">
        <div className="pay-fact">
          <dt>Cash net</dt>
          <dd className="pay-num">{day.cash_net_display}</dd>
        </div>
        <div className="pay-fact">
          <dt>Counted</dt>
          {/* null until every till is counted — "Not counted", never ₹0.00. */}
          <dd className="pay-num">{formatInrOrUnset(day.counted_cash_paise, 'Not counted')}</dd>
        </div>
        <div className={`pay-fact${day.variance_paise ? ' pay-fact--variance' : ''}`}>
          <dt>Variance</dt>
          <dd className="pay-num">{formatInrOrUnset(day.variance_paise, 'Not counted')}</dd>
        </div>
        <div className="pay-fact">
          <dt>Waiting on approval</dt>
          <dd>{plural(day.open_refund_count, 'refund')}, {plural(day.open_discount_count, 'discount')}</dd>
        </div>
      </dl>
    </section>
  )
}

/* ── Panel ───────────────────────────────────────────────────────────────── */

export default function PaymentsPanel() {
  const [tab, setTab] = useState<TabId>('counter')
  const [reloadToken, setReloadToken] = useState(0)
  const [toast, setToast] = useState<{ message: string; tone: 'ok' | 'danger' } | null>(null)

  const notify = useCallback((message: string, tone: 'ok' | 'danger' = 'ok') => {
    setToast({ message, tone })
  }, [])

  const bump = useCallback(() => setReloadToken(t => t + 1), [])

  return (
    <div className="pay">
      <header className="pay__header">
        <h1 className="pay__title">Payments</h1>
        <p className="pay__subtitle">
          Money taken, held, and given back. Receipts are documents: they are reversed,
          never edited.
        </p>
      </header>

      <div className="pay__tabs" role="tablist" aria-label="Payments sections">
        {TABS.map(t => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`pay-tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`pay-panel-${t.id}`}
            className={`pay__tab${tab === t.id ? ' pay__tab--active' : ''}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {toast && (
        <p
          className={`pay-alert pay-alert--${toast.tone === 'danger' ? 'danger' : 'ok'}`}
          role="status"
        >
          {toast.message}
        </p>
      )}

      <div
        className="pay__body"
        role="tabpanel"
        id={`pay-panel-${tab}`}
        aria-labelledby={`pay-tab-${tab}`}
      >
        {tab === 'counter' && <CounterTab onRecorded={bump} notify={notify} />}
        {tab === 'receipts' && <ReceiptsTab reloadToken={reloadToken} notify={notify} />}
        {tab === 'approvals' && <ApprovalsTab reloadToken={reloadToken} notify={notify} />}
        {tab === 'dayend' && <DayEndTab reloadToken={reloadToken} />}
      </div>
    </div>
  )
}
