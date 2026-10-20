/**
 * Pharmacy store, Ward stock (grn-21): the pharmacy's side of ward imprest
 * stores. Requests from the wards (issue indents), the decision on each, the
 * FEFO issue plan from the dispensing pharmacy, sending it, and every ward
 * store against its stock list.
 *
 * Discovered by the panel through ./viewRegistry.ts; shown to anyone holding
 * pharmacy_store.read. Behind ward stock as well as the module
 * (ORB_PHARMACY_WARD_STOCK, off until Samuel confirms ruling GRN-RS5): with it
 * off this view says so and asks the server nothing.
 *
 * THE SERVER DECIDES. Which drugs a ward may hold (its stock list, never NDPS
 * stock, never a barred high-alert drug or concentrated electrolyte), who
 * decides (never the person who asked), the FEFO plan, and that the ward
 * receives in its own name (never the person who sent it; the pharmacy cannot
 * receive for the ward). Every refusal is shown in the server's words.
 *
 * AN APPROVED REQUEST NEVER STICKS. Issuing names its lines: a line the ward
 * may no longer hold is never sent, a line short of stock sends nothing unless
 * the pharmacist types what there is, and the rest goes. A request is issued
 * once, so what is left out is not sent later; it closes when the ward
 * receives the delivery and the ward can then ask again. When nothing can go,
 * the desk cancels the request with a reason, which frees its drugs for a new
 * request at once (the ward itself may withdraw only before the decision).
 *
 * No patient data is read or shown. Expiry dates are calendar keys
 * (formatClinicalDateKey); recorded times are bare UTC from the server and go
 * through formatClinicalDateTime.
 */
import { useCallback, useEffect, useState } from 'react'

import { getUser } from '../../../api/client'
import { toPharmacyStoreError } from '../../../api/pharmacyStore'
import { cancelTransfer, dispatchTransfer } from '../../../api/pharmacyStore/transfers'
import {
  WARD_INDENT_PRIORITY_LABELS,
  WARD_INDENT_STATUS_LABELS,
  cancelWardIndent,
  decideWardIndent,
  getWardIssuePlan,
  getWardStock,
  issueWardIndent,
  listWardIndents,
  newIssueRequestKey,
  raiseWardIndent,
  type IssuePlan,
  type IssuePlanLine,
  type WardIndent,
  type WardIndentFilter,
  type WardIndentPriority,
  type WardIndentStatus,
  type WardStockOverview,
  type WardStoreStock,
} from '../../../api/pharmacyStore/ward'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../../lib/datetime'
import { PHARMACY_STORE_APPROVE, PHARMACY_STORE_WRITE, hasPermission } from '../../../lib/roles'
import type { PharmacyStoreViewProps } from './viewRegistry'
import './stockAndPurchasing.css'

export const viewId = 'ward-stock'
export const viewLabel = 'Ward stock'
export const viewOrder = 100
export const viewPermission = 'pharmacy_store.read'

type Section = 'requests' | 'stores' | 'raise'
type Fail = (err: unknown) => string

function useFail(onModuleOff: () => void): Fail {
  return useCallback((err: unknown) => {
    const failure = toPharmacyStoreError(err)
    if (failure.isModuleDisabled) onModuleOff()
    const text = failure.message || 'Something went wrong. Nothing was saved.'
    return text.charAt(0).toUpperCase() + text.slice(1)
  }, [onModuleOff])
}

const FILTERS: { value: WardIndentFilter; label: string }[] = [
  { value: 'submitted', label: 'Waiting for a decision' },
  { value: 'open', label: 'All open' },
  { value: 'closed', label: 'Received' },
  { value: '', label: 'All' },
]

function statusTone(status: WardIndentStatus): string {
  if (status === 'rejected' || status === 'cancelled') return 'psw-chip--danger'
  if (status === 'closed') return 'psw-chip--ok'
  return 'psw-chip--warn'
}

function isWhole(text: string): boolean {
  return /^\d+$/.test(text.trim())
}

/** A plan line that cannot go at all: the drug may no longer be ward stock,
 *  or it is gone from the drug master. Short stock is not this: what the
 *  pharmacy holds can still go. */
function blockedLine(line: IssuePlanLine): boolean {
  return line.problem !== null && line.problem.code !== 'insufficient_stock'
}

/** The most one issue may send on a line: what is still to send, never more
 *  than the dispensing pharmacy holds usable. */
function sendableUnits(line: IssuePlanLine): number {
  if (blockedLine(line)) return 0
  return Math.max(Math.min(line.remaining_units, line.usable_units), 0)
}

/** What each line sends unless the pharmacist changes it: everything still to
 *  send on a line with no problem, nothing on any other line. A short line
 *  sends what there is only when the pharmacist types it. */
function defaultSendUnits(plan: IssuePlan): Record<number, string> {
  return Object.fromEntries(plan.lines.map(l => [l.line_no,
    l.problem === null ? String(sendableUnits(l)) : '0']))
}

/** The batches the first `units` of a line leave by. The server's plan is
 *  earliest expiry first, a lot taken whole before the next, so fewer units
 *  take the start of the same plan. */
function batchesFor(line: IssuePlanLine, units: number): string {
  const parts: string[] = []
  let left = units
  for (const p of line.plan) {
    if (left <= 0) break
    const take = Math.min(left, p.units)
    parts.push(`${take} from batch ${p.batch_no ?? 'not recorded'}`
      + (p.expiry_date ? ` (expires ${formatClinicalDateKey(p.expiry_date)})` : ''))
    left -= take
  }
  return parts.join('; ')
}

export default function WardStockView({ meta, onModuleOff }: PharmacyStoreViewProps) {
  const features = (meta.features ?? {}) as Record<string, unknown>
  const on = features.pharmacy_ward_stock === true
  const [section, setSection] = useState<Section>('requests')
  const fail = useFail(onModuleOff)
  const canWrite = hasPermission(PHARMACY_STORE_WRITE)
  const canDecide = canWrite || hasPermission(PHARMACY_STORE_APPROVE)

  if (!on) {
    return (
      <div className="psw-view">
        <p className="psw-notice" role="status">
          Ward stock is switched off on this site. Wards are supplied by patient
          dispensing only until it is switched on.
        </p>
      </div>
    )
  }

  const sections: { id: Section; label: string }[] = [
    { id: 'requests', label: 'Requests from wards' },
    { id: 'stores', label: 'Ward stores' },
    ...(canWrite ? [{ id: 'raise' as Section, label: 'Ask for a ward' }] : []),
  ]

  return (
    <div className="psw-view">
      <nav className="psw-sections" aria-label="Ward stock sections">
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
      {section === 'requests' && (
        <RequestsSection fail={fail} canWrite={canWrite} canDecide={canDecide} />
      )}
      {section === 'stores' && <StoresSection fail={fail} />}
      {section === 'raise' && canWrite && (
        <RaiseSection fail={fail} onRaised={() => setSection('requests')} />
      )}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Requests from wards
// ═════════════════════════════════════════════════════════════════════════════

function RequestsSection({ fail, canWrite, canDecide }: {
  fail: Fail
  canWrite: boolean
  canDecide: boolean
}) {
  const [filter, setFilter] = useState<WardIndentFilter>('submitted')
  const [rows, setRows] = useState<WardIndent[] | null>(null)
  const [error, setError] = useState('')
  const [openId, setOpenId] = useState<number | null>(null)
  // Kept here, not in the detail: a decided request can leave this filter's
  // list on the reload, and its detail with it, but the outcome must stay said.
  const [notice, setNotice] = useState('')

  const load = useCallback(() => {
    listWardIndents({ status: filter }).then(
      res => { setError(''); setRows(res.indents) },
      err => setError(fail(err)),
    )
  }, [fail, filter])

  useEffect(() => { load() }, [load])

  const open = rows?.find(r => r.id === openId) ?? null

  return (
    <section className="psw-card" aria-labelledby="psw-ward-requests-title">
      <div className="psw-card__head">
        <h2 id="psw-ward-requests-title" className="psw-card__title">Requests from wards</h2>
        <div className="psw-toolbar">
          <label htmlFor="psw-ward-filter" className="psw-visually-hidden">Show requests</label>
          <select id="psw-ward-filter" value={filter}
                  onChange={e => { setOpenId(null); setFilter(e.target.value as WardIndentFilter) }}>
            {FILTERS.map(f => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </div>
      </div>
      <p className="psw-hint">
        A ward asks only for drugs on its stock list. The person who asked never
        decides, and the ward receives what you send in its own name.
      </p>
      {notice && <p className="psw-ok" role="status">{notice}</p>}
      {error && <p className="psw-error" role="alert">{error}</p>}
      {rows === null && !error && <p className="psw-note" role="status">Loading requests</p>}
      {rows && rows.length === 0 && <p className="psw-note">No requests here.</p>}
      {rows && rows.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                <th scope="col">Request</th>
                <th scope="col">Ward store</th>
                <th scope="col">Status</th>
                <th scope="col">Drugs</th>
                <th scope="col"><span className="psw-visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.id} className={r.id === openId ? 'psw-row--open' : undefined}>
                  <td>
                    <span className="psw-strong">{r.indent_number ?? 'Not numbered yet'}</span>
                    <span className="psw-sub">
                      {r.requested_by_name ?? r.requested_by ?? 'Not recorded'}
                      {r.requested_at ? `, ${formatClinicalDateTime(r.requested_at)}` : ''}
                    </span>
                  </td>
                  <td>
                    {r.store_name ?? r.store_code}
                    {r.store_department && <span className="psw-sub">{r.store_department}</span>}
                  </td>
                  <td>
                    <span className={`psw-chip ${statusTone(r.status)}`}>
                      {WARD_INDENT_STATUS_LABELS[r.status] ?? r.status_label}
                    </span>
                    {r.priority !== 'routine' && (
                      <span className="psw-sub">{WARD_INDENT_PRIORITY_LABELS[r.priority]}</span>
                    )}
                  </td>
                  <td>{r.lines.map(l => l.drug_name).join(', ')}</td>
                  <td className="psw-actions-cell">
                    <button type="button" className="psw-btn psw-btn--quiet"
                            aria-expanded={r.id === openId}
                            onClick={() => setOpenId(r.id === openId ? null : r.id)}>
                      {r.id === openId ? 'Close' : 'Open'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open && (
        // Keyed on the status and the live transfer, so a cancelled draft
        // gives the next issue a fresh request key.
        <RequestDetail key={`${open.id}-${open.status}-${open.live_transfer?.id ?? 'none'}`}
                       indent={open} fail={fail}
                       canWrite={canWrite} canDecide={canDecide}
                       onChanged={(done: string) => { setNotice(done); load() }} />
      )}
    </section>
  )
}

function RequestDetail({ indent, fail, canWrite, canDecide, onChanged }: {
  indent: WardIndent
  fail: Fail
  canWrite: boolean
  canDecide: boolean
  onChanged: (done: string) => void
}) {
  const me = getUser()?.id ?? null
  const [approve, setApprove] = useState<Record<number, string>>(
    () => Object.fromEntries(indent.lines.map(l => [l.line_no, String(l.requested_units)])))
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [plan, setPlan] = useState<IssuePlan | null>(null)
  // Units to send now, per line. A change makes a fresh request key: the key
  // stops a double press drafting twice, never a different choice of units.
  const [send, setSend] = useState<Record<number, string>>({})
  const [issueKey, setIssueKey] = useState(() => newIssueRequestKey())
  const [cancelling, setCancelling] = useState(false)
  const [cancelReason, setCancelReason] = useState('')

  const mine = me !== null && indent.requested_by === me
  const deciding = canDecide && indent.status === 'submitted' && !mine
  const issuing = canWrite && (indent.status === 'approved' || indent.status === 'partially_approved')
  const live = indent.live_transfer

  useEffect(() => {
    if (!issuing) return
    let alive = true
    getWardIssuePlan(indent.id).then(
      res => { if (alive) { setPlan(res); setSend(defaultSendUnits(res)) } },
      err => { if (alive) setError(fail(err)) },
    )
    return () => { alive = false }
  }, [indent.id, issuing, fail])

  // One row per approved line: what may go, what the pharmacist chose, and
  // whether that is a whole number within it. A line nothing can go on has
  // no box and sends nothing.
  const sendRows = (plan?.lines ?? []).map(line => {
    const most = sendableUnits(line)
    const text = send[line.line_no] ?? '0'
    const valid = most === 0 || (isWhole(text) && Number(text) <= most)
    const units = most > 0 && valid ? Number(text) : 0
    return { line, most, text, valid, units }
  })
  const toSend = sendRows.filter(r => r.units > 0)
  const sendValid = sendRows.every(r => r.valid)
  const leftOut = sendRows.filter(r => r.line.remaining_units > 0 && r.units < r.line.remaining_units)

  const act = async (work: () => Promise<unknown>, done: string) => {
    setBusy(true)
    setError('')
    try {
      await work()
      onChanged(done)
    } catch (err) {
      setError(fail(err))
    } finally {
      setBusy(false)
    }
  }

  const approvalsValid = indent.lines.every(l => isWhole(approve[l.line_no] ?? ''))

  return (
    <div className="psw-detail">
      <dl className="psw-facts">
        <dt>Ward store</dt>
        <dd>{indent.store_name ?? indent.store_code}</dd>
        <dt>Asked by</dt>
        <dd>{indent.requested_by_name ?? indent.requested_by ?? 'Not recorded'}</dd>
        <dt>From</dt>
        <dd>{indent.issuing_store_name ?? 'The dispensing pharmacy'}</dd>
        {indent.approved_by_name && (<><dt>Decided by</dt><dd>{indent.approved_by_name}</dd></>)}
        {indent.rejected_reason && (<><dt>Reason</dt><dd>{indent.rejected_reason}</dd></>)}
        {!indent.number_series_configured && (
          <><dt>Number</dt><dd>A placeholder until the site sets up its request numbers</dd></>
        )}
      </dl>
      {error && <p className="psw-error" role="alert">{error}</p>}

      <div className="psw-table-scroll">
        <table className="psw-table">
          <thead>
            <tr>
              <th scope="col">Drug</th>
              <th scope="col" className="psw-num">Asked</th>
              <th scope="col" className="psw-num">Approved</th>
              <th scope="col" className="psw-num">Sent</th>
            </tr>
          </thead>
          <tbody>
            {indent.lines.map(l => (
              <tr key={l.line_no}>
                <td>{l.drug_name}</td>
                <td className="psw-num">{l.requested_units}</td>
                <td className="psw-num">
                  {deciding ? (
                    <div className="psw-field">
                      <input aria-label={`Units of ${l.drug_name} to approve`} inputMode="numeric"
                             value={approve[l.line_no] ?? ''}
                             onChange={e => setApprove(a => ({ ...a, [l.line_no]: e.target.value }))} />
                    </div>
                  ) : (l.approved_units ?? 'Not decided')}
                </td>
                <td className="psw-num">{l.issued_units}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {canDecide && indent.status === 'submitted' && mine && (
        <p className="psw-note">You asked for this, so someone else in the pharmacy decides it.</p>
      )}
      {deciding && (
        <div className="psw-form">
          <div className="psw-field psw-field--wide">
            <label htmlFor={`psw-ward-reason-${indent.id}`}>Reason (needed to refuse)</label>
            <input id={`psw-ward-reason-${indent.id}`} value={reason}
                   onChange={e => setReason(e.target.value)} />
          </div>
          <div className="psw-buttons">
            <button type="button" className="psw-btn psw-btn--primary"
                    disabled={busy || !approvalsValid}
                    onClick={() => void act(() => decideWardIndent(indent.id, {
                      approvals: indent.lines.map(l => ({
                        line_no: l.line_no, units: Number(approve[l.line_no]) })),
                      reason: reason.trim() || null,
                    }), 'Decision recorded.')}>
              Approve
            </button>
            <button type="button" className="psw-btn"
                    disabled={busy || !reason.trim()}
                    onClick={() => void act(() => decideWardIndent(indent.id, {
                      reject: true, reason: reason.trim() }), 'Request refused.')}>
              Refuse
            </button>
          </div>
        </div>
      )}

      {issuing && !live && plan && (
        <div className="psw-form">
          <p className="psw-hint">
            Taken from {plan.from_store.name}, earliest expiry first. Nothing moves until
            you send it.
          </p>
          <div className="psw-table-scroll">
            <table className="psw-table">
              <thead>
                <tr>
                  <th scope="col">Drug</th>
                  <th scope="col" className="psw-num">Still to send</th>
                  <th scope="col" className="psw-num">Send now</th>
                  <th scope="col">Batches</th>
                </tr>
              </thead>
              <tbody>
                {sendRows.map(({ line, most, text, valid, units }) => (
                  <tr key={line.line_no}>
                    <td>{line.drug_name}</td>
                    <td className="psw-num">{line.remaining_units}</td>
                    <td className="psw-num">
                      {most > 0 ? (
                        <div className="psw-field">
                          <input aria-label={`Units of ${line.drug_name} to send now`}
                                 inputMode="numeric" value={text}
                                 aria-invalid={valid ? undefined : true}
                                 onChange={e => {
                                   setSend(s => ({ ...s, [line.line_no]: e.target.value }))
                                   setIssueKey(newIssueRequestKey())
                                 }} />
                        </div>
                      ) : 'None'}
                    </td>
                    <td>
                      {blockedLine(line) && line.problem ? (
                        <>
                          <span className="psw-error">{line.problem.message}</span>
                          <span className="psw-sub">Cannot be sent.</span>
                        </>
                      ) : line.remaining_units === 0 ? (
                        <span className="psw-sub">Nothing left to send</span>
                      ) : !valid ? (
                        <span className="psw-error">{`Send a whole number, ${most} at most.`}</span>
                      ) : line.problem ? (
                        <>
                          <span className="psw-error">
                            {line.usable_units > 0
                              ? `Only ${line.usable_units} usable in ${plan.from_store.name}.`
                              : `None usable in ${plan.from_store.name}.`}
                          </span>
                          <span className="psw-sub">
                            {units > 0
                              ? 'Batches are picked earliest expiry first when you prepare it.'
                              : 'Not sent now.'}
                          </span>
                        </>
                      ) : units === 0 ? (
                        <span className="psw-sub">Not sent now.</span>
                      ) : (
                        <span className="psw-sub">{batchesFor(line, units)}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {sendRows.every(r => r.most === 0) ? (
            <p className="psw-note">
              Nothing on this request can be sent now. Cancel the request so the ward can
              ask again.
            </p>
          ) : toSend.length > 0 && leftOut.length > 0 ? (
            <p className="psw-note">
              {`Not all of it goes now: ${leftOut.map(r => r.line.drug_name).join(', ')}. `
                + 'A request is sent once, so the rest is not sent later. The ward can ask '
                + 'for it again once this delivery is received.'}
            </p>
          ) : null}
          <div className="psw-buttons">
            <button type="button" className="psw-btn psw-btn--primary"
                    disabled={busy || !sendValid || toSend.length === 0}
                    onClick={() => void act(() => issueWardIndent(indent.id, {
                      lines: toSend.map(r => ({ line_no: r.line.line_no, units: r.units })),
                      idempotency_key: issueKey }), 'Draft ready. Send it when it is packed.')}>
              Prepare to send
            </button>
          </div>
        </div>
      )}

      {issuing && !live && (cancelling ? (
        <div className="psw-form">
          <div className="psw-field psw-field--wide">
            <label htmlFor={`psw-ward-cancel-${indent.id}`}>Reason for cancelling</label>
            <input id={`psw-ward-cancel-${indent.id}`} value={cancelReason}
                   onChange={e => setCancelReason(e.target.value)} />
          </div>
          <div className="psw-buttons">
            <button type="button" className="psw-btn"
                    disabled={busy || !cancelReason.trim()}
                    onClick={() => void act(() => cancelWardIndent(indent.id, cancelReason.trim()),
                      'Request cancelled. The ward can ask for these drugs again.')}>
              Confirm cancelling
            </button>
            <button type="button" className="psw-btn psw-btn--quiet" disabled={busy}
                    onClick={() => { setCancelling(false); setCancelReason('') }}>
              Keep the request
            </button>
          </div>
        </div>
      ) : (
        <div className="psw-buttons">
          <button type="button" className="psw-btn" disabled={busy}
                  onClick={() => setCancelling(true)}>
            Cancel the request
          </button>
        </div>
      ))}

      {live && live.status === 'draft' && canWrite && (
        <div className="psw-buttons">
          <button type="button" className="psw-btn psw-btn--primary" disabled={busy}
                  onClick={() => void act(() => dispatchTransfer(live.id), 'Sent to the ward.')}>
            Send to the ward
          </button>
          <button type="button" className="psw-btn" disabled={busy}
                  onClick={() => void act(() => cancelTransfer(live.id, 'Not sent; planned again'),
                    'Draft cancelled. The request is still approved.')}>
            Cancel the draft
          </button>
        </div>
      )}
      {live && live.status !== 'draft' && (
        <p className="psw-note">
          On its way to the ward ({live.status_label}). The ward receives it in its own name.
        </p>
      )}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Ward stores against their lists
// ═════════════════════════════════════════════════════════════════════════════

function StoresSection({ fail }: { fail: Fail }) {
  const [data, setData] = useState<WardStockOverview | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    getWardStock().then(
      res => { if (alive) { setError(''); setData(res) } },
      err => { if (alive) setError(fail(err)) },
    )
    return () => { alive = false }
  }, [fail])

  return (
    <section className="psw-card" aria-labelledby="psw-ward-stores-title">
      <div className="psw-card__head">
        <h2 id="psw-ward-stores-title" className="psw-card__title">Ward stores</h2>
      </div>
      <p className="psw-hint">
        A ward store holds only drugs on its stock list (set as par levels in Masters).
        Ward stock is never dispensed by Helix.
      </p>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {data === null && !error && <p className="psw-note" role="status">Loading ward stores</p>}
      {data && data.stores.length === 0 && (
        <p className="psw-note">No ward store is set up yet. Add one in Masters.</p>
      )}
      {data && data.stores.map(store => <StoreTable key={store.store.id} store={store} />)}
    </section>
  )
}

function StoreTable({ store }: { store: WardStoreStock }) {
  return (
    <div className="psw-detail">
      <p className="psw-strong">
        {store.store.name}
        {store.store.department ? `, ${store.store.department}` : ''}
        {!store.store.is_active ? ' (closed)' : ''}
      </p>
      {store.drugs.length === 0 ? (
        <p className="psw-note">No stock list and no stock yet.</p>
      ) : (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                <th scope="col">Drug</th>
                <th scope="col" className="psw-num">Par</th>
                <th scope="col" className="psw-num">On hand</th>
                <th scope="col" className="psw-num">On its way</th>
                <th scope="col" className="psw-num">Asked for</th>
                <th scope="col">Notes</th>
              </tr>
            </thead>
            <tbody>
              {store.drugs.map(d => (
                <tr key={d.drug_id}>
                  <td>
                    {d.drug_name}
                    {d.earliest_expiry && (
                      <span className="psw-sub">Earliest expiry {formatClinicalDateKey(d.earliest_expiry)}</span>
                    )}
                  </td>
                  <td className="psw-num">{d.listed ? d.par_units : 'Not listed'}</td>
                  <td className="psw-num">{d.on_hand_units}</td>
                  <td className="psw-num">{d.in_transit_units}</td>
                  <td className="psw-num">{d.requested_units}</td>
                  <td>
                    {d.listed && d.below_par && <span className="psw-chip psw-chip--warn">Below par</span>}
                    {d.held_units > 0 && <span className="psw-sub">{d.held_units} held</span>}
                    {d.expired_units > 0 && <span className="psw-sub">{d.expired_units} expired</span>}
                    {(d.below_zero_units ?? 0) > 0 && (
                      <span className="psw-chip psw-chip--warn">
                        {`${d.below_zero_units} given with none on the books, count this drug`}
                      </span>
                    )}
                    {d.problem && <span className="psw-sub">{d.problem.message}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Asking on a ward's behalf
// ═════════════════════════════════════════════════════════════════════════════

function RaiseSection({ fail, onRaised }: { fail: Fail; onRaised: () => void }) {
  const [data, setData] = useState<WardStockOverview | null>(null)
  const [error, setError] = useState('')
  const [storeCode, setStoreCode] = useState('')
  const [amounts, setAmounts] = useState<Record<string, string>>({})
  const [priority, setPriority] = useState<WardIndentPriority>('routine')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let alive = true
    getWardStock().then(
      res => { if (alive) { setError(''); setData(res) } },
      err => { if (alive) setError(fail(err)) },
    )
    return () => { alive = false }
  }, [fail])

  const store = data?.stores.find(s => s.store.code === storeCode) ?? null
  const askable = (store?.drugs ?? []).filter(d => d.listed && !d.problem)
  const lines = askable
    .filter(d => isWhole(amounts[d.drug_id] ?? '') && Number(amounts[d.drug_id]) > 0)
    .map(d => ({ helix_medication_id: d.drug_id, quantity_units: Number(amounts[d.drug_id]) }))

  const send = async () => {
    if (!store) return
    setBusy(true)
    setError('')
    try {
      await raiseWardIndent({ store_code: store.store.code, lines, priority })
      onRaised()
    } catch (err) {
      setError(fail(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="psw-card" aria-labelledby="psw-ward-raise-title">
      <div className="psw-card__head">
        <h2 id="psw-ward-raise-title" className="psw-card__title">Ask on a ward&apos;s behalf</h2>
      </div>
      <p className="psw-hint">
        For a ward that cannot ask itself. Someone else in the pharmacy then decides it.
      </p>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {data === null && !error && <p className="psw-note" role="status">Loading ward stores</p>}
      {data && (
        <div className="psw-form">
          <div className="psw-fields">
            <div className="psw-field">
              <label htmlFor="psw-ward-store">Ward store</label>
              <select id="psw-ward-store" value={storeCode}
                      onChange={e => { setStoreCode(e.target.value); setAmounts({}) }}>
                <option value="">Choose a ward store</option>
                {data.stores.filter(s => s.store.is_active).map(s => (
                  <option key={s.store.code} value={s.store.code}>{s.store.name}</option>
                ))}
              </select>
            </div>
            <div className="psw-field">
              <label htmlFor="psw-ward-priority">Priority</label>
              <select id="psw-ward-priority" value={priority}
                      onChange={e => setPriority(e.target.value as WardIndentPriority)}>
                {data.priorities.map(p => (
                  <option key={p} value={p}>{WARD_INDENT_PRIORITY_LABELS[p] ?? p}</option>
                ))}
              </select>
            </div>
          </div>
          {store && askable.length === 0 && (
            <p className="psw-note">Nothing on this store&apos;s list can be asked for.</p>
          )}
          {store && askable.length > 0 && (
            <ul className="psw-lines">
              {askable.map(d => (
                <li key={d.drug_id} className="psw-line">
                  <span className="psw-strong">{d.drug_name}</span>
                  <span className="psw-sub">{`${d.on_hand_units} on hand of ${d.par_units}`}</span>
                  <div className="psw-field">
                    <label htmlFor={`psw-ward-units-${d.drug_id}`}>Units</label>
                    <input id={`psw-ward-units-${d.drug_id}`} inputMode="numeric"
                           value={amounts[d.drug_id] ?? ''}
                           placeholder={d.top_up_units ? String(d.top_up_units) : '0'}
                           onChange={e => setAmounts(a => ({ ...a, [d.drug_id]: e.target.value }))} />
                  </div>
                </li>
              ))}
            </ul>
          )}
          <div className="psw-buttons">
            <button type="button" className="psw-btn psw-btn--primary"
                    disabled={busy || !store || lines.length === 0}
                    onClick={() => void send()}>
              Send the request
            </button>
          </div>
        </div>
      )}
    </section>
  )
}
