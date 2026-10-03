/**
 * Orb — the Insurance / TPA desk.
 *
 * ╔════════════════════════════════════════════════════════════════════════╗
 * ║ THIS SCREEN SENDS NOTHING TO ANY PAYER, AND SAYS SO, ALWAYS.           ║
 * ╚════════════════════════════════════════════════════════════════════════╝
 *
 * Orb has no connection to any TPA, insurer, government scheme or clearing
 * house. Every button here records what a person did somewhere else — on the
 * payer's own portal, by e-mail, or on paper. The notice is rendered before the
 * first network round trip, is not dismissible, and repeats on every action
 * form that could otherwise be mistaken for a transmission.
 *
 * That is not decoration. The failure it prevents is silent and expensive: a
 * clerk who believes Orb submitted the claim does not submit the claim, and by
 * the time anyone checks, the payer's window has closed and the hospital eats
 * the bill.
 *
 * WHY THE WORK QUEUE IS THE DEFAULT VIEW. A TPA desk does not read a list of
 * claims; it reads what is stuck and for how long. The queue is ordered oldest
 * first, bucketed the way a desk escalates, and split by whose move it is —
 * because "the payer has had this for 40 days" and "we have not answered their
 * query for 40 days" are the same number and completely different mornings.
 *
 * MONEY. Integer paise end to end. Amounts are rendered from the server's own
 * `*_display` strings wherever the server computed them, so the screen can
 * never disagree with the record it just created; anything the client derives
 * goes through src/lib/money.ts. An amount nobody has entered is `null` and
 * reads as words — never "₹0.00", which means something else entirely.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  addNote, cancelClaim, capturePolicy, closeClaim, createClaim, getClaim, getClaims,
  getDenials, getMeta, getPayers, getPolicies, getSummary, getWorkQueue,
  recordAppeal, recordDecision, recordPreauthDecision, recordPreauthRequest,
  recordQuery, recordSettlement, recordSubmission, savePayer, setClaimedAmount,
  STATUS_LABEL, statusTone, verifyPolicy, whoseMove,
  type Claim, type ClaimStatus, type DenialLog, type DeskSummary, type Payer,
  type PayerMeta, type Policy, type WorkQueue,
} from '../../api/payer'
import { mrnText, newIdempotencyKey } from '../../api/payments'
import PatientPicker from '../../components/common/PatientPicker'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../lib/datetime'
import { MoneyError, toPaise } from '../../lib/money'
import './PayerPanel.css'

/* ── The notice, duplicated on purpose ──────────────────────────────────────
 * The server sends this same sentence on every payload, and the panel prefers
 * the server's copy once it has one. This literal exists so the words are on
 * screen in the FIRST PAINT — before any fetch resolves, and still there if
 * every fetch fails. The one message that must never be missing cannot be one
 * that depends on the network being up.
 */
const NOTICE =
  'Orb does not connect to any TPA, insurer or government scheme. '
  + 'Nothing on this screen is transmitted to a payer. '
  + 'Recording a pre-authorisation, a submission or a settlement here records '
  + "what the desk did elsewhere, on the payer's portal, by e-mail or on paper."

const NOTICE_SHORT = 'Recorded in Orb only, not sent to the payer.'

type View = 'queue' | 'claims' | 'denials' | 'policies' | 'payers'

const VIEWS: { id: View; label: string }[] = [
  { id: 'queue', label: 'Work queue' },
  { id: 'claims', label: 'All claims' },
  { id: 'denials', label: 'Denials' },
  { id: 'policies', label: 'Patient cover' },
  { id: 'payers', label: 'Payers' },
]

/* ── Small presentational pieces ─────────────────────────────────────────── */

// The same words the payer form's own options use.
const PAYER_TYPE_LABEL: Record<string, string> = {
  self_pay: 'Self-pay',
  tpa: 'TPA',
  insurer: 'Insurer',
  pmjay: 'PM-JAY',
  state_scheme: 'State scheme',
  cghs: 'CGHS',
  esic: 'ESIC',
  corporate: 'Corporate',
  charity: 'Charity',
}

const SUBMISSION_MODE_LABEL: Record<string, string> = {
  portal: "the payer's portal",
  email: 'e-mail',
  paper: 'paper',
  api: "the payer's own API",
}

// Server refusals arrive in lower case ("cannot raise a claim..."); a banner
// starts with a capital like every other sentence on the page.
function sentence(text: string): string {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text
}

function StatusBadge({ status }: { status: ClaimStatus }) {
  return (
    <span className={`payer-badge payer-badge--${statusTone(status)}`}>
      {STATUS_LABEL[status] ?? status}
    </span>
  )
}

function MoveChip({ status }: { status: ClaimStatus }) {
  const move = whoseMove(status)
  const label = move === 'payer' ? 'Payer to act' : move === 'us' ? 'Ours to move' : 'Finished'
  return <span className={`payer-move payer-move--${move}`}>{label}</span>
}

/** Days pending, with the thing it is counted FROM named beside it — an aging
 *  number with no anchor is the number people misread. */
function Age({ claim }: { claim: Claim }) {
  if (claim.age_days === null) {
    return <span className="payer-age payer-age--unknown">Age unknown</span>
  }
  return (
    <span className={`payer-age${claim.is_stale ? ' payer-age--stale' : ''}`}>
      <span className="payer-age__n tabular-nums">{claim.age_days}</span>
      <span className="payer-age__unit">{claim.age_days === 1 ? 'day' : 'days'}</span>
      <span className="payer-age__since">since {claim.aging_anchor_label.toLowerCase()}</span>
    </span>
  )
}

/* ── A claim nobody has priced yet ──────────────────────────────────────────
 *
 * `claimed_paise` is the ONE money column on a claim that is NOT NULLABLE
 * (migrations.py, `claimed_paise INTEGER NOT NULL DEFAULT 0`), so "the desk has
 * not entered what we claimed" is written to the database as 0 and then
 * formatted as ₹0.00 — the exact thing the ClaimSettlement docstring forbids:
 * "a claim awaiting a decision and a claim approved for nothing look identical
 * as zeros and mean opposite things."
 *
 * ₹0 is not a real claim value here: payer_service refuses to record a
 * submission for ₹0 ("enter what was claimed first"), so a stored zero can
 * only mean not-entered. Say that in words. The proper fix is a nullable column
 * plus a `claimed_is_configured` flag like every sibling field has; this keeps
 * ₹0.00 off the screen until that lands.
 */
const CLAIM_AMOUNT_UNSET = 'Not entered'

function claimedText(paise: number | null | undefined, display: string): string {
  return paise ? display : CLAIM_AMOUNT_UNSET
}

function Money({ value, display, tone }: { value: number | null; display: string; tone?: 'ok' | 'warn' | 'danger' }) {
  const unset = value === null
  return (
    <span
      className={
        'payer-money tabular-nums'
        + (unset ? ' payer-money--unset' : '')
        + (tone && !unset ? ` payer-money--${tone}` : '')
      }
    >
      {display}
    </span>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="payer-field">
      <span className="payer-field__label">{label}</span>
      {children}
    </label>
  )
}

/** Rupee input. Parsing happens ONCE, here, through lib/money — never
 *  parseFloat. An unparseable amount is reported, not silently coerced to 0. */
function RupeeInput({
  value, onChange, id, placeholder,
}: { value: string; onChange: (v: string) => void; id?: string; placeholder?: string }) {
  return (
    <span className="payer-rupee">
      <span className="payer-rupee__symbol" aria-hidden="true">₹</span>
      <input
        id={id}
        className="payer-input payer-input--money tabular-nums"
        inputMode="decimal"
        value={value}
        placeholder={placeholder ?? '0.00'}
        onChange={e => onChange(e.target.value)}
      />
    </span>
  )
}

function parseRupees(text: string): number {
  // Throws MoneyError on anything that is not a number — the caller shows it.
  return toPaise(text.trim())
}

/* ── The claim drawer ────────────────────────────────────────────────────── */

/* ── One component per recorded action ──────────────────────────────────────
 * Each form owns its own fields. They used to share one amount, one reference
 * and one text box across the whole drawer, so typing a note for the file
 * filled the payer query and the denial reason too, and enabled "Record a
 * denial" on the strength of a note. */

/** Runs one recorded action; true when the server accepted it, so the form
 *  that asked can clear its own fields. */
type RunAction = (fn: () => Promise<{ claim: Claim }>) => Promise<boolean>

interface ActionProps {
  claim: Claim
  busy: boolean
  run: RunAction
  onError: (message: string) => void
}

/** Parses a rupee amount once, through lib/money, or reports why it could not. */
function readRupees(raw: string, onError: (message: string) => void): number | null {
  try {
    return parseRupees(raw)
  } catch (err) {
    onError(err instanceof MoneyError ? `That amount could not be read: ${err.message}` : String(err))
    return null
  }
}

function PreauthRequestAction({ claim, busy, run, onError }: ActionProps) {
  const [amount, setAmount] = useState('')
  const [reference, setReference] = useState('')
  const submit = async () => {
    const paise = readRupees(amount, onError)
    if (paise === null) return
    if (await run(() => recordPreauthRequest(claim.id, { requested_paise: paise, reference: reference || null }))) {
      setAmount(''); setReference('')
    }
  }
  return (
    <div className="payer-action">
      <Field label="Pre-authorisation amount requested">
        <RupeeInput value={amount} onChange={setAmount} />
      </Field>
      <Field label="Payer's reference (optional)">
        <input className="payer-input" value={reference} onChange={e => setReference(e.target.value)} />
      </Field>
      <button type="button" className="payer-btn" disabled={busy || !amount} onClick={() => void submit()}>
        Record pre-auth raised
      </button>
      <p className="payer-hint">{NOTICE_SHORT}</p>
    </div>
  )
}

function PreauthDecisionAction({ claim, busy, run, onError }: ActionProps) {
  const [amount, setAmount] = useState('')
  const [reference, setReference] = useState('')
  const [reason, setReason] = useState('')
  const approve = async () => {
    const paise = readRupees(amount, onError)
    if (paise === null) return
    if (await run(() => recordPreauthDecision(claim.id, {
      decision: paise === claim.preauth_requested_paise ? 'approved' : 'partially_approved',
      approved_paise: paise, reference: reference || null,
    }))) {
      setAmount(''); setReference('')
    }
  }
  const deny = async () => {
    if (await run(() => recordPreauthDecision(claim.id, {
      decision: 'denied', denial_code: 'OTHER', denial_reason: reason,
    }))) {
      setReason('')
    }
  }
  return (
    <div className="payer-action">
      <Field label="Amount the payer approved">
        <RupeeInput value={amount} onChange={setAmount} />
      </Field>
      <Field label="Pre-auth number">
        <input className="payer-input" value={reference} onChange={e => setReference(e.target.value)} />
      </Field>
      <div className="payer-action__row">
        <button type="button" className="payer-btn" disabled={busy || !amount} onClick={() => void approve()}>
          Record payer's approval
        </button>
        <button type="button" className="payer-btn payer-btn--danger" disabled={busy || !reason}
          onClick={() => void deny()}>
          Record denial
        </button>
      </div>
      <Field label="Payer's reason, in the payer's own words (required to record a denial)">
        <textarea className="payer-input payer-input--area" value={reason}
          onChange={e => setReason(e.target.value)} rows={2} />
      </Field>
    </div>
  )
}

function SubmissionAction({ claim, busy, run }: ActionProps) {
  const [reference, setReference] = useState('')
  const submit = async () => {
    if (await run(() => recordSubmission(claim.id, { external_ref: reference || null }))) setReference('')
  }
  return (
    <div className="payer-action">
      <Field label="Payer's claim / acknowledgement reference">
        <input className="payer-input" value={reference} onChange={e => setReference(e.target.value)} />
      </Field>
      <button type="button" className="payer-btn" disabled={busy} onClick={() => void submit()}>
        Record that we submitted this
      </button>
      <p className="payer-hint">
        {NOTICE_SHORT} Submit the claim on the payer&apos;s portal first, then record it here.
      </p>
    </div>
  )
}

function QueryAction({ claim, busy, run }: ActionProps) {
  const [query, setQuery] = useState('')
  const submit = async () => {
    if (await run(() => recordQuery(claim.id, { query_text: query }))) setQuery('')
  }
  return (
    <div className="payer-action">
      <Field label="What the payer asked for">
        <textarea className="payer-input payer-input--area" value={query}
          onChange={e => setQuery(e.target.value)} rows={2} />
      </Field>
      <button type="button" className="payer-btn payer-btn--quiet" disabled={busy || !query}
        onClick={() => void submit()}>
        Record a payer query
      </button>
    </div>
  )
}

function QueryAnswerAction({ claim, busy, run }: ActionProps) {
  const [answer, setAnswer] = useState('')
  const submit = async () => {
    if (await run(() => recordQuery(claim.id, { query_text: answer, answered: true }))) setAnswer('')
  }
  return (
    <div className="payer-action">
      <Field label="What we sent back">
        <textarea className="payer-input payer-input--area" value={answer}
          onChange={e => setAnswer(e.target.value)} rows={2} />
      </Field>
      <button type="button" className="payer-btn" disabled={busy || !answer} onClick={() => void submit()}>
        Record our answer
      </button>
    </div>
  )
}

function DecisionAction({ claim, busy, run, onError, meta, canDeny }: ActionProps & {
  meta: PayerMeta | null
  canDeny: boolean
}) {
  const [amount, setAmount] = useState('')
  const [tds, setTds] = useState('')
  const [denialCode, setDenialCode] = useState('OTHER')
  const [reason, setReason] = useState('')
  const decide = async () => {
    const paise = readRupees(amount, onError)
    if (paise === null) return
    let tdsPaise = 0
    if (tds) {
      const parsed = readRupees(tds, onError)
      if (parsed === null) return
      tdsPaise = parsed
    }
    if (await run(() => recordDecision(claim.id, {
      decision: paise === claim.claimed_paise ? 'approved' : 'partially_approved',
      approved_paise: paise,
      tds_paise: tdsPaise,
    }))) {
      setAmount(''); setTds('')
    }
  }
  const deny = async () => {
    if (await run(() => recordDecision(claim.id, {
      decision: 'denied', denial_code: denialCode, denial_reason: reason,
    }))) {
      setReason('')
    }
  }
  return (
    <div className="payer-action">
      <Field label="Amount the payer allowed">
        <RupeeInput value={amount} onChange={setAmount} />
      </Field>
      <Field label="TDS withheld by the payer (optional)">
        <RupeeInput value={tds} onChange={setTds} />
      </Field>
      <div className="payer-action__row">
        <button type="button" className="payer-btn" disabled={busy || !amount} onClick={() => void decide()}>
          Record the payer&apos;s decision
        </button>
        {canDeny && (
          <button type="button" className="payer-btn payer-btn--danger" disabled={busy || !reason}
            onClick={() => void deny()}>
            Record a denial
          </button>
        )}
      </div>
      {canDeny && (
        <>
          <Field label="Reason code (Orb's own tag, for counting, not the payer's code)">
            <select className="payer-input" value={denialCode}
              onChange={e => setDenialCode(e.target.value)}>
              {(meta?.denial_reason_codes ?? [{ code: 'OTHER', label: 'Other' }]).map(c => (
                <option key={c.code} value={c.code}>{c.label}</option>
              ))}
            </select>
          </Field>
          <Field label="The payer's reason, in the payer's own words (required)">
            <textarea className="payer-input payer-input--area" value={reason}
              onChange={e => setReason(e.target.value)} rows={2} />
          </Field>
        </>
      )}
    </div>
  )
}

function SettlementAction({ claim, busy, run, onError }: ActionProps) {
  const [amount, setAmount] = useState('')
  const [reference, setReference] = useState('')
  const submit = async () => {
    const paise = readRupees(amount, onError)
    if (paise === null) return
    if (await run(() => recordSettlement(claim.id, { settled_paise: paise, utr_reference: reference || null }))) {
      setAmount(''); setReference('')
    }
  }
  return (
    <div className="payer-action">
      <Field label="Amount actually received">
        <RupeeInput value={amount} onChange={setAmount} />
      </Field>
      <Field label="UTR / bank reference">
        <input className="payer-input" value={reference} onChange={e => setReference(e.target.value)} />
      </Field>
      <button type="button" className="payer-btn" disabled={busy || !amount} onClick={() => void submit()}>
        Record money received
      </button>
      <p className="payer-hint">
        Any shortfall is worked out from the approved amount, the co-pay and the
        TDS. You never type the shortfall.
      </p>
    </div>
  )
}

function AppealAction({ claim, busy, run }: ActionProps) {
  const [grounds, setGrounds] = useState('')
  const submit = async () => {
    if (await run(() => recordAppeal(claim.id, { grounds }))) setGrounds('')
  }
  return (
    <div className="payer-action">
      <Field label="Grounds for the appeal">
        <textarea className="payer-input payer-input--area" value={grounds}
          onChange={e => setGrounds(e.target.value)} rows={2} />
      </Field>
      <button type="button" className="payer-btn" disabled={busy || !grounds} onClick={() => void submit()}>
        Record an appeal
      </button>
      <p className="payer-hint">
        {NOTICE_SHORT} A denied claim can only reach a settlement through an
        appeal that somebody recorded: money cannot appear against a refusal.
      </p>
    </div>
  )
}

function ClaimAmountAction({ claim, busy, run, onError }: ActionProps) {
  const [amount, setAmount] = useState('')
  const submit = async () => {
    const paise = readRupees(amount, onError)
    if (paise === null) return
    if (await run(() => setClaimedAmount(claim.id, paise))) setAmount('')
  }
  return (
    <div className="payer-action">
      <Field label="Revise the claimed amount">
        <RupeeInput value={amount} onChange={setAmount} />
      </Field>
      <button type="button" className="payer-btn payer-btn--quiet" disabled={busy || !amount}
        onClick={() => void submit()}>
        Set claimed amount
      </button>
    </div>
  )
}

function NoteAction({ claim, busy, run }: ActionProps) {
  const [note, setNote] = useState('')
  const next = claim.legal_next_statuses
  const after = async (fn: () => Promise<{ claim: Claim }>) => {
    if (await run(fn)) setNote('')
  }
  return (
    <div className="payer-action">
      <Field label="Note for the file">
        <input className="payer-input" value={note} onChange={e => setNote(e.target.value)}
          placeholder="Called the TPA, said Tuesday" />
      </Field>
      <div className="payer-action__row">
        <button type="button" className="payer-btn payer-btn--quiet" disabled={busy || !note}
          onClick={() => void after(() => addNote(claim.id, note))}>
          Add note
        </button>
        {next.includes('closed') && (
          <button type="button" className="payer-btn payer-btn--quiet" disabled={busy}
            onClick={() => void after(() => closeClaim(claim.id, note || undefined))}>
            Close claim
          </button>
        )}
        {next.includes('cancelled') && (
          <button type="button" className="payer-btn payer-btn--quiet" disabled={busy || !note}
            onClick={() => void after(() => cancelClaim(claim.id, note))}>
            Cancel claim
          </button>
        )}
      </div>
    </div>
  )
}


interface DrawerProps {
  claim: Claim
  meta: PayerMeta | null
  onClose: () => void
  onChanged: (claim: Claim) => void
}

function ClaimDrawer({ claim, meta, onClose, onChanged }: DrawerProps) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const s = claim.settlement
  const next = claim.legal_next_statuses

  const run: RunAction = useCallback(async fn => {
    setBusy(true)
    setError(null)
    try {
      const reply = await fn()
      onChanged(reply.claim)
      return true
    } catch (err) {
      // The server's refusals are written to be read by a clerk
      // ("a claim that is 'denied' cannot become 'settled'"), so they are shown
      // verbatim rather than replaced with a generic failure.
      setError(err instanceof Error ? err.message : String(err))
      return false
    } finally {
      setBusy(false)
    }
  }, [onChanged])

  const act: ActionProps = { claim, busy, run, onError: setError }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // Portalled to document.body: a drawer rendered inside the panel inherits the
  // panel's stacking and scroll context and gets clipped by it.
  return createPortal(
    <div className="payer-drawer__scrim" role="presentation" onClick={onClose}>
      <section
        className="payer-drawer"
        role="dialog"
        aria-modal="true"
        aria-label={`Claim ${claim.claim_number ?? claim.id}`}
        onClick={e => e.stopPropagation()}
      >
        <header className="payer-drawer__head">
          <div>
            <h2 className="payer-drawer__title">{claim.claim_number ?? `Claim #${claim.id}`}</h2>
            <p className="payer-drawer__sub">
              {claim.patient_name} · {claim.payer_name ?? 'Unknown payer'} ·{' '}
              {claim.claim_type === 'reimbursement' ? 'Reimbursement' : claim.claim_type === 'scheme' ? 'Scheme' : 'Cashless'}
            </p>
          </div>
          <div className="payer-drawer__headright">
            <StatusBadge status={claim.status} />
            <button type="button" className="payer-btn payer-btn--quiet" onClick={onClose}>Close</button>
          </div>
        </header>

        <p className="payer-notice payer-notice--inline">{NOTICE_SHORT}</p>

        {claim.claim_type === 'reimbursement' && (
          <p className="payer-callout">
            Reimbursement: the patient pays the hospital in full and recovers from the
            insurer themselves. Nothing here is a hospital receivable.
          </p>
        )}

        {!claim.settlement_matches_record && (
          <p className="payer-callout payer-callout--warn">
            The policy has changed since this decision was recorded. The stored figure
            is the record of what was true then; the breakdown below is a live
            recomputation and they no longer agree.
          </p>
        )}

        {/* ── The arithmetic, laid out so a clerk can check it by hand ──
            Every line is a subtraction from the one above it, in the order the
            payer's own remittance advice states them. */}
        <div className="payer-breakdown">
          <h3 className="payer-h3">What was claimed, what was allowed, what arrived</h3>
          <dl className="payer-ledger">
            <div className="payer-ledger__row">
              <dt>Claimed from payer</dt>
              {/* `claimed_paise` is never null (NOT NULL DEFAULT 0), so Money's
                  own unset path can never fire for it — see claimedText(). */}
              <dd><Money value={s.claimed_paise || null} display={claimedText(s.claimed_paise, s.claimed_display)} /></dd>
            </div>
            <div className="payer-ledger__row">
              <dt>Payer allowed</dt>
              <dd><Money value={s.approved_paise} display={s.approved_display} /></dd>
            </div>
            <div className="payer-ledger__row">
              <dt>Disallowed by payer</dt>
              <dd><Money value={s.disallowed_paise} display={s.disallowed_display} tone="warn" /></dd>
            </div>
            <div className="payer-ledger__row">
              <dt>Patient co-pay{s.copay_percent_bp ? ` (${s.copay_percent_bp / 100}%)` : ''}</dt>
              <dd><Money value={s.copay_paise} display={s.copay_display} /></dd>
            </div>
            <div className="payer-ledger__row">
              <dt>TDS withheld by payer</dt>
              <dd><Money value={s.tds_paise} display={s.tds_display} /></dd>
            </div>
            <div className="payer-ledger__row payer-ledger__row--rule">
              <dt>Expected from payer</dt>
              <dd><Money value={s.expected_receipt_paise} display={s.expected_receipt_display} /></dd>
            </div>
            <div className="payer-ledger__row">
              <dt>Received</dt>
              <dd><Money value={s.settled_paise} display={s.settled_display} /></dd>
            </div>
            <div className="payer-ledger__row payer-ledger__row--total">
              <dt>{s.is_over_settled ? 'Over-settled by' : 'Short-settled by'}</dt>
              <dd>
                <Money
                  value={s.short_settled_paise}
                  display={s.short_settled_display}
                  tone={s.is_short_settled ? 'danger' : undefined}
                />
              </dd>
            </div>
            <div className="payer-ledger__row payer-ledger__row--rule">
              <dt>Patient owes the hospital</dt>
              <dd><Money value={s.patient_liability_paise} display={s.patient_liability_display} /></dd>
            </div>
          </dl>
        </div>

        {error && <p className="payer-error" role="alert">{sentence(error)}</p>}

        {/* ── Actions, offered only where the lifecycle allows them ──
            The buttons are derived from the server's own transition table, so a
            move the server would refuse is never on screen. */}
        <div className="payer-actions">
          <h3 className="payer-h3">Record what happened</h3>

          {next.includes('preauth_requested') && <PreauthRequestAction {...act} />}
          {claim.status === 'preauth_requested' && <PreauthDecisionAction {...act} />}
          {next.includes('submitted') && <SubmissionAction {...act} />}
          {next.includes('queried') && <QueryAction {...act} />}
          {claim.status === 'queried' && <QueryAnswerAction {...act} />}
          {(next.includes('approved') || next.includes('partially_approved')) && (
            <DecisionAction {...act} meta={meta} canDeny={next.includes('denied')} />
          )}
          {next.includes('settled') && <SettlementAction {...act} />}
          {next.includes('appealed') && <AppealAction {...act} />}
          {(claim.status === 'draft' || claim.status === 'preauth_requested'
            || claim.status === 'preauth_approved' || claim.status === 'queried') && (
            <ClaimAmountAction {...act} />
          )}
          <NoteAction {...act} />
        </div>

        {/* ── The file's history. Two clocks: when the desk did it, and when Orb
            heard about it. That pair is what makes a submission date defensible
            when a payer disputes it. */}
        <div className="payer-history">
          <h3 className="payer-h3">History</h3>
          <ol className="payer-timeline">
            {(claim.events ?? []).map(ev => (
              <li key={ev.id} className="payer-timeline__item">
                <span className="payer-timeline__when tabular-nums">
                  {/* Stored as bare UTC; slicing it printed the UTC clock. */}
                  {formatClinicalDateTime(ev.occurred_at ?? ev.recorded_at)}
                </span>
                <span className="payer-timeline__what">
                  <strong>{ev.event_type.replace(/_/g, ' ')}</strong>
                  {ev.to_status && ev.from_status && ev.to_status !== ev.from_status && (
                    <>: {STATUS_LABEL[ev.from_status as ClaimStatus] ?? ev.from_status} →{' '}
                      {STATUS_LABEL[ev.to_status as ClaimStatus] ?? ev.to_status}</>
                  )}
                  {ev.amount_paise !== null && (
                    <span className="payer-timeline__amount tabular-nums"> {ev.amount_display}</span>
                  )}
                  {ev.denial_reason && <em className="payer-timeline__reason"> {ev.denial_reason}</em>}
                  {ev.remark && <span className="payer-timeline__remark"> {ev.remark}</span>}
                </span>
              </li>
            ))}
            {(claim.events ?? []).length === 0 && (
              <li className="payer-timeline__item payer-timeline__item--empty">Nothing recorded yet.</li>
            )}
          </ol>
        </div>
      </section>
    </div>,
    document.body,
  )
}

/* ── Claim row ───────────────────────────────────────────────────────────── */

function ClaimRow({ claim, onOpen }: { claim: Claim; onOpen: (c: Claim) => void }) {
  return (
    <button type="button" className="payer-row" onClick={() => onOpen(claim)}>
      <span className="payer-row__lead">
        <StatusBadge status={claim.status} />
        <MoveChip status={claim.status} />
      </span>
      <span className="payer-row__body">
        <span className="payer-row__primary">{claim.patient_name}</span>
        <span className="payer-row__secondary">
          {claim.claim_number ?? `#${claim.id}`} · {claim.payer_name ?? 'Unknown payer'}
          {claim.policy_number ? ` · ${claim.policy_number}` : ''}
        </span>
      </span>
      <span className="payer-row__trailing">
        <span
          className={'payer-row__value tabular-nums' + (claim.claimed_paise ? '' : ' payer-money--unset')}
        >
          {claimedText(claim.claimed_paise, claim.claimed_display)}
        </span>
        <Age claim={claim} />
      </span>
    </button>
  )
}

/* ── The panel ───────────────────────────────────────────────────────────── */

export default function PayerPanel() {
  const [view, setView] = useState<View>('queue')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState(NOTICE)
  const [meta, setMeta] = useState<PayerMeta | null>(null)
  const [summary, setSummary] = useState<DeskSummary | null>(null)
  const [queue, setQueue] = useState<WorkQueue | null>(null)
  const [claims, setClaims] = useState<Claim[]>([])
  const [denials, setDenials] = useState<DenialLog | null>(null)
  const [payers, setPayers] = useState<Payer[]>([])
  const [policies, setPolicies] = useState<Policy[]>([])
  const [selected, setSelected] = useState<Claim | null>(null)
  const [bucket, setBucket] = useState<string | null>(null)

  /* Deliberately touches NO state before its first await. The mount effect
     calls this, and a setState in an effect BODY is a cascading render — and,
     if the panel unmounted while the fetch was in flight, a write into a dead
     tree. `loading` already starts true, so the mount path needs no pre-flight
     flag; the Refresh button raises it itself, from an event handler, where it
     is free to. */
  const load = useCallback(async () => {
    try {
      const [m, s, q] = await Promise.all([getMeta(), getSummary(), getWorkQueue()])
      setMeta(m)
      setSummary(s)
      setQueue(q)
      if (q.notice) setNotice(q.notice)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void load() }, [load])

  useEffect(() => {
    if (view === 'claims') void getClaims().then(r => setClaims(r.claims)).catch(() => setClaims([]))
    if (view === 'denials') void getDenials().then(setDenials).catch(() => setDenials(null))
    if (view === 'policies') void getPolicies().then(r => setPolicies(r.policies)).catch(() => setPolicies([]))
    // The payer directory is loaded for THREE views, not just its own: the new
    // claim form and the cover form both choose a payer from it, and a required
    // dropdown that is empty until you happen to visit another tab is a dead
    // form.
    if (view === 'payers' || view === 'claims' || view === 'policies') {
      void getPayers().then(r => setPayers(r.payers)).catch(() => setPayers([]))
    }
  }, [view])

  const openClaim = useCallback(async (claim: Claim) => {
    setSelected(claim)
    try {
      const full = await getClaim(claim.id)
      setSelected(full.claim)
    } catch {
      /* keep the summary row we already have; the drawer degrades, not breaks */
    }
  }, [])

  const onClaimChanged = useCallback((claim: Claim) => {
    setSelected(claim)
    void load()
    setClaims(prev => prev.map(c => (c.id === claim.id ? claim : c)))
  }, [load])

  const visibleQueue = useMemo(() => {
    if (!queue) return []
    return bucket ? queue.claims.filter(c => c.age_bucket === bucket) : queue.claims
  }, [queue, bucket])

  return (
    <div className="payer-panel">
      <header className="payer-head">
        <div>
          <h1 className="payer-title">Insurance &amp; claims</h1>
          <p className="payer-sub">The TPA desk: cover on file, pre-authorisations, claims and what is stuck.</p>
        </div>
        <button
          type="button"
          className="payer-btn payer-btn--quiet"
          onClick={() => { setLoading(true); void load() }}
        >
          Refresh
        </button>
      </header>

      {/* Undismissable, above everything, on every view. */}
      <p className="payer-notice" role="note">{notice}</p>

      {summary && (
        <div className="payer-metrics">
          <div className="payer-metric">
            <span className="payer-metric__value tabular-nums">{summary.open_claims}</span>
            <span className="payer-metric__label">Open claims</span>
          </div>
          {/* A ZERO ADVERSE COUNT RENDERS NEUTRAL — the rule the shell states at
              ClinicianShell.css `.orb-count-chip`, applied here. "0 stuck", "0
              denied" and "₹0.00 short-settled" are the DESK'S GOOD DAY, and
              painting them amber and red is how a colour stops meaning anything
              by the second week. The tone is now earned by the number. */}
          <div className="payer-metric">
            <span className={`payer-metric__value tabular-nums${summary.stale_claims > 0 ? ' payer-metric__value--warn' : ''}`}>{summary.stale_claims}</span>
            <span className="payer-metric__label">Stuck &gt; 15 days</span>
          </div>
          <div className="payer-metric">
            <span className="payer-metric__value tabular-nums">{summary.awaiting_preauth}</span>
            <span className="payer-metric__label">Pre-auth pending</span>
          </div>
          <div className="payer-metric">
            <span className={`payer-metric__value tabular-nums${summary.denied_open > 0 ? ' payer-metric__value--danger' : ''}`}>{summary.denied_open}</span>
            <span className="payer-metric__label">Denied, not resolved</span>
          </div>
          <div className="payer-metric">
            <span className="payer-metric__value tabular-nums">{summary.confirmed_receivable_display}</span>
            <span className="payer-metric__label">Approved, not yet received</span>
          </div>
          <div className="payer-metric">
            <span className="payer-metric__value tabular-nums">{summary.estimated_receivable_display}</span>
            {/* Labelled an estimate because it is one: these claims have no
                decision yet, so the figure is what was claimed, not what is owed. */}
            <span className="payer-metric__label">Claimed, no decision yet</span>
          </div>
          <div className="payer-metric">
            <span className={`payer-metric__value tabular-nums${summary.short_settled_count > 0 ? ' payer-metric__value--danger' : ''}`}>
              {summary.short_settled_display}
            </span>
            <span className="payer-metric__label">
              Short-settled ({summary.short_settled_count})
            </span>
          </div>
        </div>
      )}

      <div className="payer-tabs" role="tablist" aria-label="Insurance desk views">
        {VIEWS.map(v => (
          <button
            key={v.id}
            type="button"
            role="tab"
            aria-selected={view === v.id}
            className={`payer-tab${view === v.id ? ' payer-tab--active' : ''}`}
            // A refusal belongs to the tab it was raised on; carried to the
            // next tab it read as that tab's problem.
            onClick={() => { setView(v.id); setError(null) }}
          >
            {v.label}
          </button>
        ))}
      </div>

      {error && <p className="payer-error" role="alert">{sentence(error)}</p>}
      {loading && <p className="payer-empty">Loading the desk…</p>}

      {view === 'queue' && queue && (
        <section className="payer-section">
          <div className="payer-buckets">
            <button
              type="button"
              className={`payer-bucket${bucket === null ? ' payer-bucket--active' : ''}`}
              onClick={() => setBucket(null)}
            >
              <span className="payer-bucket__n tabular-nums">{queue.total}</span>
              <span className="payer-bucket__label">All open</span>
            </button>
            {queue.buckets.map(b => (
              <button
                key={b.bucket}
                type="button"
                className={`payer-bucket${bucket === b.bucket ? ' payer-bucket--active' : ''}`}
                onClick={() => setBucket(bucket === b.bucket ? null : b.bucket)}
              >
                <span className="payer-bucket__n tabular-nums">{b.count}</span>
                <span className="payer-bucket__label">{b.bucket} days</span>
                <span className="payer-bucket__value tabular-nums">{b.claimed_display}</span>
              </button>
            ))}
          </div>
          <p className="payer-split">
            <strong className="tabular-nums">{queue.awaiting_payer}</strong> waiting on the payer ·{' '}
            <strong className="tabular-nums">{queue.awaiting_hospital}</strong> waiting on us
          </p>
          <ul className="payer-list">
            {visibleQueue.map(c => (
              <li key={c.id}><ClaimRow claim={c} onOpen={openClaim} /></li>
            ))}
            {visibleQueue.length === 0 && (
              <li className="payer-empty">Nothing is stuck. The queue is empty.</li>
            )}
          </ul>
        </section>
      )}

      {view === 'claims' && (
        <section className="payer-section">
          <NewClaimForm payers={payers} onCreated={c => { setClaims(p => [c, ...p]); void load() }} />
          <ul className="payer-list">
            {claims.map(c => <li key={c.id}><ClaimRow claim={c} onOpen={openClaim} /></li>)}
            {claims.length === 0 && <li className="payer-empty">No claims yet.</li>}
          </ul>
        </section>
      )}

      {view === 'denials' && denials && (
        <section className="payer-section">
          <p className="payer-hint">{denials.codes_are_local}</p>
          <div className="payer-buckets">
            {denials.by_code.map(c => (
              <div key={c.denial_code} className="payer-bucket payer-bucket--static">
                <span className="payer-bucket__n tabular-nums">{c.count}</span>
                <span className="payer-bucket__label">{c.label}</span>
                <span className="payer-bucket__value tabular-nums">{c.amount_display}</span>
              </div>
            ))}
            {denials.by_code.length === 0 && <p className="payer-empty">No denials recorded.</p>}
          </div>
          <ul className="payer-list">
            {denials.denials.map(d => (
              <li key={d.id} className="payer-denial">
                <span className="payer-denial__head">
                  <strong>{d.patient_name}</strong> · {d.claim_number ?? `#${d.claim_id}`} ·{' '}
                  {d.payer_name ?? 'Unknown payer'}
                </span>
                <span className="payer-denial__code">{d.denial_code_label}</span>
                <span className="payer-denial__reason">{d.denial_reason}</span>
                <span className="payer-denial__amount tabular-nums">{d.amount_at_stake_display}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {view === 'policies' && (
        <section className="payer-section">
          <PolicyForm
            payers={payers}
            onCaptured={p => setPolicies(prev => [p, ...prev])}
          />
          <ul className="payer-list">
            {policies.map(p => (
              <li key={p.id} className="payer-policy">
                <span className="payer-policy__head">
                  {/* Name and MRN, as a claim shows them; the bare id only when
                      the id names no registered patient. */}
                  <strong>{p.patient_name ?? `ID ${p.patient_id}`}</strong>
                  {p.patient_mrn ? ` · ${mrnText(p.patient_mrn)}` : ''}
                  {' · '}{p.payer_name ?? 'Unknown payer'}
                  {p.is_primary && <span className="payer-badge payer-badge--info">Primary</span>}
                </span>
                <span className="payer-policy__meta">
                  Policy {p.policy_number ?? 'not recorded'} · co-pay {p.copay_display}
                  {p.valid_to ? ` · valid to ${formatClinicalDateKey(p.valid_to)}` : ''}
                </span>
                <span className={`payer-policy__sum tabular-nums${p.sum_insured_is_configured ? '' : ' payer-money--unset'}`}>
                  Sum insured: {p.sum_insured_display}
                </span>
                {!p.verified_at && (
                  <button type="button" className="payer-btn payer-btn--quiet"
                    onClick={() => void verifyPolicy(p.id).then(r => setPolicies(prev =>
                      prev.map(x => (x.id === r.policy.id ? r.policy : x))))}>
                    Mark verified with payer
                  </button>
                )}
              </li>
            ))}
            {policies.length === 0 && (
              <li className="payer-empty">
                No cover on file. The admission form does not capture insurance. It is
                captured here, after the patient is admitted.
              </li>
            )}
          </ul>
        </section>
      )}

      {view === 'payers' && (
        <section className="payer-section">
          <PayerForm payers={payers} onSaved={p => setPayers(prev => [p, ...prev.filter(x => x.id !== p.id)])} />
          <ul className="payer-list">
            {payers.map(p => (
              <li key={p.id} className="payer-payer">
                <span className="payer-payer__name"><strong>{p.name}</strong> <span>{p.code}</span></span>
                <span className="payer-payer__meta">
                  {PAYER_TYPE_LABEL[p.payer_type] ?? p.payer_type.replace(/_/g, ' ')}
                  {p.claim_submission_mode
                    ? ` · submits by ${SUBMISSION_MODE_LABEL[p.claim_submission_mode] ?? p.claim_submission_mode}`
                    : ''}
                  {p.preauth_required ? ' · pre-auth required' : ''}
                  {/* Credit terms belong to a payer who settles later; self-pay
                      has none, and an unset term is said in words, not as
                      "credit Not configured". */}
                  {p.payer_type === 'self_pay' ? ''
                    : p.credit_days == null ? ' · credit terms not set'
                      : ` · credit ${p.credit_days_display}`}
                </span>
              </li>
            ))}
            {payers.length === 0 && <li className="payer-empty">No payers configured.</li>}
          </ul>
        </section>
      )}

      {selected && (
        <ClaimDrawer
          claim={selected}
          meta={meta}
          onClose={() => setSelected(null)}
          onChanged={onClaimChanged}
        />
      )}
    </div>
  )
}

/* ── Forms ───────────────────────────────────────────────────────────────── */

/** One in-flight guard and one idempotency key per form. The key lives until
 *  the form succeeds or its fields change, so a double click or a retry after a
 *  lost reply repeats the SAME request (the server answers with the claim it
 *  already opened), while an edited form is a new request. */
function useOnceSubmit() {
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)
  const key = useRef<string | null>(null)
  const changed = useCallback(() => { key.current = null }, [])
  /** `fn` resolves true when the save went through; only then is the key spent. */
  const submit = useCallback(async (fn: (idempotencyKey: string) => Promise<boolean>) => {
    // A ref, not only `busy`: two clicks inside one frame both see busy=false.
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    key.current ??= newIdempotencyKey()
    try {
      if (await fn(key.current)) key.current = null
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }, [])
  return { busy, submit, changed }
}

function NewClaimForm({ payers, onCreated }: { payers: Payer[]; onCreated: (c: Claim) => void }) {
  const [patientId, setPatientId] = useState('')
  const [payerCode, setPayerCode] = useState('')
  const [amount, setAmount] = useState('')
  const [claimType, setClaimType] = useState<'cashless' | 'reimbursement' | 'scheme'>('cashless')
  const [error, setError] = useState<string | null>(null)
  const { busy, submit, changed } = useOnceSubmit()
  const claimable = payers.filter(p => p.claimable)
  const edit = <T,>(set: (v: T) => void) => (v: T) => { changed(); set(v) }

  return (
    <form
      className="payer-form"
      onSubmit={e => {
        e.preventDefault()
        setError(null)
        void submit(async idempotencyKey => {
          if (!patientId.trim()) {
            setError('Choose the patient this claim is for.')
            return false
          }
          try {
            const paise = amount ? parseRupees(amount) : null
            const reply = await createClaim({
              patient_id: patientId.trim(),
              claim_type: claimType,
              payer_code: payerCode || null,
              claimed_paise: paise,
              idempotency_key: idempotencyKey,
            })
            onCreated(reply.claim)
            setPatientId(''); setAmount('')
            return true
          } catch (err) {
            setError(err instanceof Error ? err.message : String(err))
            return false
          }
        })
      }}
    >
      <h3 className="payer-h3">Open a claim file</h3>
      <div className="payer-form__grid">
        <PatientPicker
          value={patientId}
          onChange={edit(setPatientId)}
          hint="Search by name, bed or MRN. For a discharged patient, enter the ID."
        />
        <Field label="Payer (blank uses the patient's policy)">
          <select className="payer-input" value={payerCode} onChange={e => edit(setPayerCode)(e.target.value)}>
            <option value="">From the patient&apos;s cover</option>
            {claimable.map(p => <option key={p.id} value={p.code}>{p.name}</option>)}
          </select>
        </Field>
        <Field label="Type">
          <select className="payer-input" value={claimType}
            onChange={e => edit(setClaimType)(e.target.value as 'cashless' | 'reimbursement' | 'scheme')}>
            <option value="cashless">Cashless</option>
            <option value="reimbursement">Reimbursement (patient recovers)</option>
            <option value="scheme">Government scheme</option>
          </select>
        </Field>
        <Field label="Amount claimed (optional now)">
          <RupeeInput value={amount} onChange={edit(setAmount)} />
        </Field>
      </div>
      {error && <p className="payer-error" role="alert">{sentence(error)}</p>}
      <button type="submit" className="payer-btn" disabled={busy}>Open claim</button>
    </form>
  )
}

function PolicyForm({ payers, onCaptured }: { payers: Payer[]; onCaptured: (p: Policy) => void }) {
  const [patientId, setPatientId] = useState('')
  const [payerCode, setPayerCode] = useState('')
  const [policyNumber, setPolicyNumber] = useState('')
  const [sumInsured, setSumInsured] = useState('')
  const [copay, setCopay] = useState('')
  const [validTo, setValidTo] = useState('')
  const [error, setError] = useState<string | null>(null)
  // The policy route takes no key, so the in-flight guard is what stops a
  // double click recording the same cover twice.
  const { busy, submit } = useOnceSubmit()
  const claimable = payers.filter(p => p.claimable)

  return (
    <form
      className="payer-form"
      onSubmit={e => {
        e.preventDefault()
        setError(null)
        void submit(async () => {
          if (!patientId.trim()) {
            setError('Choose the patient this cover belongs to.')
            return false
          }
          // Checked here: "ten" went out as NaN and came back as "API error:
          // 422", and 150 came back naming a field in basis points.
          const copayPct = copay.trim() ? Number(copay.trim().replace(/%$/, '')) : 0
          if (!Number.isFinite(copayPct) || copayPct < 0 || copayPct > 100) {
            setError('Co-pay must be a number from 0 to 100.')
            return false
          }
          try {
            const reply = await capturePolicy({
              patient_id: patientId.trim(),
              payer_code: payerCode,
              policy_number: policyNumber || null,
              // Blank means NOT KNOWN and is sent as null. It is never sent as 0:
              // a hospital that reads ₹0 of cover as a fact refuses a cashless
              // admission it should have accepted.
              sum_insured_paise: sumInsured ? parseRupees(sumInsured) : null,
              copay_percent_bp: Math.round(copayPct * 100),
              valid_to: validTo || null,
            })
            onCaptured(reply.policy)
            setPatientId(''); setPolicyNumber(''); setSumInsured(''); setCopay(''); setValidTo('')
            return true
          } catch (err) {
            setError(err instanceof Error ? err.message : String(err))
            return false
          }
        })
      }}
    >
      <h3 className="payer-h3">Record a patient&apos;s cover</h3>
      <p className="payer-hint">
        The admission form captures no insurance. An admission must never wait on a
        card. Cover is recorded here, afterwards.
      </p>
      <div className="payer-form__grid">
        <PatientPicker
          value={patientId}
          onChange={setPatientId}
          hint="Search by name, bed or MRN. For a discharged patient, enter the ID."
        />
        <Field label="Payer">
          <select className="payer-input" value={payerCode}
            onChange={e => setPayerCode(e.target.value)} required>
            <option value="">Choose…</option>
            {claimable.map(p => <option key={p.id} value={p.code}>{p.name}</option>)}
          </select>
        </Field>
        <Field label="Policy / card number">
          <input className="payer-input" value={policyNumber} onChange={e => setPolicyNumber(e.target.value)} />
        </Field>
        <Field label="Sum insured (leave blank if not known)">
          <RupeeInput value={sumInsured} onChange={setSumInsured} placeholder="Not known" />
        </Field>
        <Field label="Co-pay %">
          <input className="payer-input tabular-nums" inputMode="decimal" value={copay}
            onChange={e => setCopay(e.target.value)} placeholder="0" />
        </Field>
        <Field label="Valid to">
          <input className="payer-input" type="date" value={validTo} onChange={e => setValidTo(e.target.value)} />
        </Field>
      </div>
      {error && <p className="payer-error" role="alert">{sentence(error)}</p>}
      <button type="submit" className="payer-btn" disabled={busy}>Record cover</button>
    </form>
  )
}

function PayerForm({ payers, onSaved }: { payers: Payer[]; onSaved: (p: Payer) => void }) {
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [type, setType] = useState('tpa')
  const [mode, setMode] = useState('portal')
  const [preauth, setPreauth] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { busy, submit } = useOnceSubmit()
  const existing = payers.find(p => p.code === code.trim().toUpperCase()) ?? null

  // Typing a saved payer's code loads its values, so an update starts from what
  // is on file rather than from this form's defaults. The save sends only these
  // five fields; the server keeps every other column, the tariff link included.
  const onCode = (value: string) => {
    setCode(value)
    const match = payers.find(p => p.code === value.trim().toUpperCase())
    if (match) {
      setName(match.name)
      setType(match.payer_type)
      setMode(match.claim_submission_mode ?? '')
      setPreauth(match.preauth_required)
    }
  }

  return (
    <form
      className="payer-form"
      onSubmit={e => {
        e.preventDefault()
        setError(null)
        void submit(async () => {
          try {
            const reply = await savePayer({
              code: code.trim().toUpperCase(),
              name: name.trim(),
              payer_type: type as Payer['payer_type'],
              claim_submission_mode: mode || null,
              preauth_required: preauth,
            })
            onSaved(reply.payer)
            setCode(''); setName('')
            return true
          } catch (err) {
            setError(err instanceof Error ? err.message : String(err))
            return false
          }
        })
      }}
    >
      <h3 className="payer-h3">Add or update a payer</h3>
      <div className="payer-form__grid">
        <Field label="Code"><input className="payer-input" value={code} onChange={e => onCode(e.target.value)} required /></Field>
        <Field label="Name"><input className="payer-input" value={name} onChange={e => setName(e.target.value)} required /></Field>
        <Field label="Type">
          <select className="payer-input" value={type} onChange={e => setType(e.target.value)}>
            <option value="tpa">TPA</option>
            <option value="insurer">Insurer</option>
            <option value="pmjay">PM-JAY</option>
            <option value="state_scheme">State scheme</option>
            <option value="cghs">CGHS</option>
            <option value="esic">ESIC</option>
            <option value="corporate">Corporate</option>
            <option value="charity">Charity</option>
          </select>
        </Field>
        <Field label="How the desk submits (outside Orb)">
          <select className="payer-input" value={mode} onChange={e => setMode(e.target.value)}>
            <option value="">Not recorded</option>
            <option value="portal">Payer&apos;s portal</option>
            <option value="email">E-mail</option>
            <option value="paper">Paper</option>
            <option value="api">Payer&apos;s own API (used by a person, not by Orb)</option>
          </select>
        </Field>
        <Field label="Pre-authorisation required">
          <input type="checkbox" className="payer-check" checked={preauth}
            onChange={e => setPreauth(e.target.checked)} />
        </Field>
      </div>
      {existing && (
        <p className="payer-hint">
          Updating {existing.code}. Its tariff plan
          ({existing.tariff_plan_code ?? 'none linked'}), contacts and credit terms are
          kept as saved.
        </p>
      )}
      {error && <p className="payer-error" role="alert">{sentence(error)}</p>}
      <button type="submit" className="payer-btn" disabled={busy}>Save payer</button>
      <p className="payer-hint">
        {NOTICE_SHORT} Recording a submission mode does not give Orb a way to use it.
      </p>
    </form>
  )
}
