/**
 * Critical results: the laboratory's side of a critical value (lis-15).
 *
 * WHAT IS LISTED. Every critical result still open, the most overdue first,
 * then those closed in the last day. A critical is a value past a limit the
 * pathologist approved (Orb ships none, ruling LIS-R1); the laboratory's own
 * validation raised it, marked preliminary until it is authorised (LIS-R4
 * (a)), and Orb posted it to the patient's case room as "Laboratory".
 *
 * WHAT CLOSES ONE (ruling LIS-R3). Only a callback logged here, naming the
 * person told and their role, how they were told, and that they read the
 * value back. A post in the case room, or any reply to it, never closes it,
 * and neither does discharge or a transfer: a discharged patient's critical
 * stays here with "Patient discharged" and the instruction to phone the
 * discharging consultant (LIS-R19). A call that reached nobody is logged as
 * an attempt and the critical stays open. Each call names the value this
 * screen showed as told to the ward; when the value changed after the list
 * loaded, the server logs nothing and the list reloads, so a read-back is
 * never recorded against a value nobody phoned. The form is bound to one
 * value: the read-back tick records the value it was ticked for and the call
 * sends that value, and a reload (the timer, Refresh, or the reload after a
 * refusal) that brings another value, or any further change to it, clears
 * the form, so a tick given for one value can never close a critical for
 * the next.
 *
 * DELIVERY is said in words: posted in the case room, or not delivered (no
 * case room, the room closed at discharge, a failed post) with the
 * instruction to phone. Not delivered means the tier is due at once.
 *
 * THE LADDER is the site's own (site.toml [lab.critical]); with none set,
 * every critical is due at once and the screen says so. The last tier never
 * hands on, so a critical that climbed the whole ladder unconfirmed is due
 * from the moment it got there: it is listed with the most overdue, styled
 * as due and counted in "due now".
 *
 * AN AMENDED OR WITHDRAWN RESULT is followed to the version that stands now:
 * "Result now" and the value told are the newest version's, and once the
 * result is withdrawn the call is about the withdrawal.
 *
 * The list read is audited, so it loads when the view opens, after each
 * step, on request, and once a minute while open; the due times are
 * redrawn every 30 seconds without asking the server. No score and no
 * severity of Orb's own: the laboratory's value and the agreed limit only.
 * Critical results stay reachable while the module drains.
 */
import { useCallback, useEffect, useState } from 'react'

import { toLabError } from '../../../api/lab'
import {
  getCriticals,
  logCriticalAttempt,
  logCriticalCallback,
  type CriticalList,
  type CriticalNotification,
} from '../../../api/lab/criticals'
import { formatClinicalDate, formatClinicalDateTime, formatClinicalTime, parseUTC } from '../../../lib/datetime'
import type { LabViewProps } from './viewRegistry'
import './LabBench.css'

export const viewId = 'critical'
export const viewLabel = 'Critical results'
export const viewOrder = 60
export const viewPermission = 'lab.read'
export const viewWhenDraining = 'show'

const POLL_MS = 60_000
const CLOCK_MS = 30_000

/** Refusals that mean the screen is out of date: reload the list.
 *  'value_changed': the result was validated again with another value after
 *  the list loaded, so the call was about a value that no longer stands. */
const STALE_CODES = new Set(['already_closed', 'not_found', 'value_changed'])

/** The value told once the result was withdrawn (the server's
 *  lab_critical_service.WITHDRAWN_TOLD): the call is about the withdrawal. */
const WITHDRAWN_TOLD = 'withdrawn'

function isDue(n: CriticalNotification, now: Date): boolean {
  if (n.status !== 'open') return false
  if (!n.tier.due_at) return n.tier.due_now
  try {
    return parseUTC(n.tier.due_at).getTime() <= now.getTime()
  } catch {
    return true
  }
}

function patientLine(n: CriticalNotification): string {
  const p = n.patient
  const parts = [p.name ?? 'Unknown patient', p.mrn ? `UHID ${p.mrn}` : null]
  if (p.discharged) {
    parts.push(p.discharged_at ? `Patient discharged ${formatClinicalDate(p.discharged_at)}` : 'Patient discharged')
  } else {
    const where = [p.ward, p.bed ? `Bed ${p.bed}` : null].filter(Boolean).join(', ')
    if (where) parts.push(where)
  }
  return parts.filter(Boolean).join(' · ')
}

function tierLine(n: CriticalNotification, now: Date): string {
  if (!n.tier.ladder_configured) {
    return 'No callback ladder is set for this site: due now. Phone the ward.'
  }
  const head = `Tier ${n.tier.current} of ${n.tier.count}${n.tier.label ? `: ${n.tier.label}` : ''}`
  if (n.status !== 'open') return head
  const lastTier = n.tier.count > 0 && n.tier.current >= n.tier.count
  if (lastTier && (!n.tier.due_at || isDue(n, now))) {
    return `${head}. Due now. Last tier: keep calling until someone reads the value back.`
  }
  if (!n.tier.due_at) return `${head}. Due now.`
  if (isDue(n, now)) {
    return `${head}. Due now${n.tier.next_label ? `; next: ${n.tier.next_label}` : ''}.`
  }
  return `${head}. Next call due ${formatClinicalTime(n.tier.due_at)}${n.tier.next_label ? ` (${n.tier.next_label})` : ''}.`
}

interface CardProps {
  n: CriticalNotification
  now: Date
  canLog: boolean
  roles: Record<string, string>
  methods: Record<string, string>
  onDone: (message: string) => void
  onFailed: (message: string, stale: boolean) => void
}

/** The key that binds one callback form to one value told: a reload that
 *  brings another value, or any further change to it (even one that comes
 *  back to the same value), mounts a fresh form, so the name, the role and
 *  the read-back tick given for one value never carry over to the next.
 *  `changes` is the server's whole change record for the critical, so its
 *  length only grows. */
function callbackFormKey(n: CriticalNotification): string {
  return `${n.id}:${n.value_told}:${n.changes?.length ?? 0}`
}

/** The callback and attempt forms for one open critical. Mounted under
 *  callbackFormKey, so all of its state belongs to one value told. */
function CallbackForms({ n, roles, methods, onDone, onFailed }: Omit<CardProps, 'now' | 'canLog'>) {
  const [name, setName] = useState('')
  const [role, setRole] = useState('')
  const [method, setMethod] = useState<'phone' | 'in_person'>('phone')
  // The value on screen when the read-back box was ticked: the value the
  // read-back attests, and the one the call sends. The box shows ticked only
  // while that is still the value told.
  const [readBackFor, setReadBackFor] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [tried, setTried] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const label = `${n.test_name}, ${n.patient.name ?? 'patient'}`
  const readBack = readBackFor !== null && readBackFor === n.value_told

  const log = async () => {
    if (busy) return
    if (name.trim().length < 2) { setError('Type the name of the person you told.'); return }
    if (!role) { setError('Choose the role of the person you told.'); return }
    if (readBackFor === null || readBackFor !== n.value_told) {
      setError('Ask them to read the value back to you before you log the call.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      // value_told: the value on screen when the read-back was ticked, which
      // the server checks against the value the ward has been told before it
      // closes anything.
      await logCriticalCallback(n.id, {
        recipient_name: name.trim(), recipient_role: role, method, read_back: true,
        note: note.trim() || null, value_told: readBackFor,
      })
      onDone(`${n.test_name} for ${n.patient.name ?? 'the patient'}: callback logged and closed.`)
    } catch (err) {
      const failure = toLabError(err)
      setError(failure.message || 'The callback could not be logged.')
      onFailed(`${n.test_name}: ${failure.message}`, STALE_CODES.has(failure.code))
    } finally {
      setBusy(false)
    }
  }

  const attempt = async () => {
    if (busy) return
    if (tried.trim().length < 3) { setError('Say who you tried and what happened.'); return }
    setBusy(true)
    setError(null)
    try {
      await logCriticalAttempt(n.id, { method, note: tried.trim(), value_told: n.value_told })
      setTried('')
      onDone(`${n.test_name}: the call was logged. The result stays open.`)
    } catch (err) {
      const failure = toLabError(err)
      setError(failure.message || 'The call could not be logged.')
      onFailed(`${n.test_name}: ${failure.message}`, STALE_CODES.has(failure.code))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="lab-bench-section">
      <p className="lab-bench-quiet">
        {n.value_told === WITHDRAWN_TOLD
          ? `This call is about the withdrawal of ${n.test_name}: tell the ward the result was entered in error and is no longer on the chart.`
          : `This call is about ${n.test_name} ${n.value_told}, the value the ward was last sent.`}
      </p>
      <div className="lab-bench-form">
        <label className="lab-bench-field">
          Person told
          <input className="lab-bench-input" aria-label={`Person told, ${label}`} value={name}
            maxLength={120} onChange={e => setName(e.target.value)} />
        </label>
        <label className="lab-bench-field">
          Their role
          <select className="lab-bench-input" aria-label={`Their role, ${label}`} value={role}
            onChange={e => setRole(e.target.value)}>
            <option value="">Choose</option>
            {Object.entries(roles).map(([id, words]) => <option key={id} value={id}>{words}</option>)}
          </select>
        </label>
        <label className="lab-bench-field">
          How
          <select className="lab-bench-input" aria-label={`How you told them, ${label}`} value={method}
            onChange={e => setMethod(e.target.value === 'in_person' ? 'in_person' : 'phone')}>
            {Object.entries(methods).map(([id, words]) => <option key={id} value={id}>{words}</option>)}
          </select>
        </label>
        <label className="lab-bench-field">
          Note
          <input className="lab-bench-input lab-bench-input--comment" aria-label={`Note, ${label}`}
            value={note} maxLength={500} onChange={e => setNote(e.target.value)} />
        </label>
      </div>
      <div className="lab-bench-actions">
        <label className="lab-bench-check">
          <input type="checkbox" checked={readBack}
            onChange={e => setReadBackFor(e.target.checked ? n.value_told : null)}
            aria-label={`They read the value back to me, ${label}`} />
          They read the value back to me
        </label>
        <button type="button" className="lab-bench-btn lab-bench-btn--primary" disabled={busy}
          onClick={() => void log()}>
          {busy ? 'Saving…' : 'Log callback and close'}
        </button>
      </div>
      <div className="lab-bench-form">
        <label className="lab-bench-field">
          A call that reached nobody
          <input className="lab-bench-input" aria-label={`Who you tried, ${label}`}
            placeholder="Who you tried and what happened" value={tried} maxLength={500}
            onChange={e => setTried(e.target.value)} />
        </label>
        <button type="button" className="lab-bench-btn" disabled={busy} onClick={() => void attempt()}>
          Log the attempt
        </button>
      </div>
      {error && <p className="lab-bench-error" role="alert">{error}</p>}
    </div>
  )
}

function CriticalCard({ n, now, canLog, roles, methods, onDone, onFailed }: CardProps) {
  const due = isDue(n, now)
  const undelivered = n.delivery.status !== 'posted' && n.delivery.status !== 'pending'
  return (
    <article className={`lab-bench-card${n.status === 'open' && (due || undelivered) ? ' lab-bench-card--stat' : ''}`}
      aria-label={`Critical result, ${n.test_name}, ${n.patient.name ?? 'patient'}`}>
      <div className="lab-bench-card-head">
        <span className="lab-bench-name">{n.test_name} {n.value_display}</span>
        {n.preliminary && n.result_now.status === 'tech_validated' && (
          <span className="lab-bench-stat">Preliminary, not yet authorised</span>
        )}
        {n.result_now.changed && <span className="lab-bench-stat">Value changed</span>}
      </div>
      <dl className="lab-bench-facts">
        <dt>Patient</dt>
        <dd>{patientLine(n)}</dd>
        {n.critical_limit_text && (<><dt>Agreed limit</dt><dd>{n.critical_limit_text}</dd></>)}
        {n.result_now.text && (<><dt>Result now</dt><dd>{n.result_now.text}</dd></>)}
        <dt>Raised</dt>
        <dd>
          {formatClinicalDateTime(n.raised_at)}{n.raised_by_name ? ` by ${n.raised_by_name}` : ''}
          {n.preliminary ? ', before authorisation' : ''}
        </dd>
        <dt>Callback</dt>
        <dd>{tierLine(n, now)}</dd>
        <dt>Case room</dt>
        <dd className={undelivered ? 'lab-bench-flag lab-bench-flag--out' : undefined}>{n.delivery.text}</dd>
        {n.acknowledged && (
          <>
            <dt>Closed</dt>
            <dd>
              {n.acknowledged.method_text ?? 'Call'} to {n.acknowledged.recipient_name}
              {n.acknowledged.recipient_role_text ? ` (${n.acknowledged.recipient_role_text})` : ''}, read back
              {n.acknowledged.at ? `, ${formatClinicalDateTime(n.acknowledged.at)}` : ''}
              {n.acknowledged.by_name ? `, logged by ${n.acknowledged.by_name}` : ''}
            </dd>
          </>
        )}
      </dl>
      {n.callbacks.length > 0 && (
        <ul className="lab-bench-tests" aria-label="Calls logged">
          {n.callbacks.map((c, i) => (
            <li key={`${c.at}-${i}`}>
              {c.at ? `${formatClinicalTime(c.at)} ` : ''}
              {c.reached
                ? `${c.method_text ?? 'Call'} to ${c.recipient_name ?? 'someone'}${c.recipient_role_text ? ` (${c.recipient_role_text})` : ''}, read back`
                : `${c.method_text ?? 'Call'} that reached nobody: ${c.note ?? ''}`}
              {c.by_name ? `, logged by ${c.by_name}` : ''}
            </li>
          ))}
        </ul>
      )}
      {n.status === 'open' && canLog && (
        <CallbackForms key={callbackFormKey(n)} n={n} roles={roles} methods={methods}
          onDone={onDone} onFailed={onFailed} />
      )}
      {n.status === 'open' && !canLog && (
        <p className="lab-bench-quiet">Laboratory staff log the callback for this result.</p>
      )}
    </article>
  )
}

export default function CriticalView({ onModuleOff }: LabViewProps) {
  const [list, setList] = useState<CriticalList | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [warning, setWarning] = useState<string | null>(null)
  const [checkedAt, setCheckedAt] = useState<Date | null>(null)
  const [now, setNow] = useState(() => new Date())

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setList(await getCriticals())
      setError(null)
      const at = new Date()
      setCheckedAt(at)
      setNow(at)
    } catch (err) {
      const failure = toLabError(err)
      if (failure.isModuleDisabled) onModuleOff()
      else setError(failure.message || 'The critical results could not be loaded.')
    } finally {
      setLoading(false)
    }
  }, [onModuleOff])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void load() }, [load])

  useEffect(() => {
    const poll = window.setInterval(() => { void load() }, POLL_MS)
    const clock = window.setInterval(() => setNow(new Date()), CLOCK_MS)
    return () => {
      window.clearInterval(poll)
      window.clearInterval(clock)
    }
  }, [load])

  const onDone = (message: string) => {
    setWarning(null)
    setNotice(message)
    void load()
  }

  const onFailed = (message: string, stale: boolean) => {
    setNotice(null)
    setWarning(stale ? `${message} The list has been reloaded.` : message)
    if (stale) void load()
  }

  const ladder = list?.ladder
  const dueCount = list ? list.open.filter(n => isDue(n, now)).length : 0

  return (
    <div className="lab-bench">
      <div className="lab-bench-head">
        <div>
          <h2 className="lab-bench-title">Critical results</h2>
          <p className="lab-bench-sub">
            Values past a limit the pathologist approved. Each stays here until the laboratory
            logs a callback with the name and role of the person told and their read-back. A post
            in the case room, or a reply to it, never closes one, and neither does discharge.
            {checkedAt ? ` Checked at ${formatClinicalTime(checkedAt)}.` : ''}
          </p>
          {ladder && (ladder.configured ? (
            <p className="lab-bench-sub">
              Callback ladder:{' '}
              {ladder.tiers.map(t => `${t.tier}. ${t.label}${t.timeout_minutes ? `, ${t.timeout_minutes} minutes` : ''}`).join('; ')}.
            </p>
          ) : (
            <p className="lab-bench-sub">
              No callback ladder is set for this site, so every critical result is due at once:
              phone the ward.
            </p>
          ))}
        </div>
        <button type="button" className="lab-bench-btn" onClick={() => void load()} disabled={loading}>
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {notice && <p className="lab-bench-note lab-bench-note--ok" role="status">{notice}</p>}
      {warning && <p className="lab-bench-note lab-bench-note--warn" role="status">{warning}</p>}

      {error && (
        <div className="lab-bench-note lab-bench-note--error" role="alert">
          <p>Couldn&apos;t load the critical results. {error}</p>
          <p>This is a display failure, not an empty list. Phone the ward for any critical value you know of.</p>
        </div>
      )}

      {list && !error && (
        <>
          <section className="lab-bench-section" aria-labelledby="lab-critical-open">
            <h3 id="lab-critical-open" className="lab-bench-section-title">
              Open ({list.open.length}){dueCount > 0 ? `, ${dueCount} due now` : ''}
            </h3>
            {list.open.length === 0 && (
              <p className="lab-bench-empty">No critical result is waiting for a callback.</p>
            )}
            {list.open.map(n => (
              <CriticalCard key={n.id} n={n} now={now} canLog={list.can_log}
                roles={list.recipient_roles} methods={list.methods}
                onDone={onDone} onFailed={onFailed} />
            ))}
          </section>

          <section className="lab-bench-section" aria-labelledby="lab-critical-recent">
            <h3 id="lab-critical-recent" className="lab-bench-section-title">
              Closed in the last {list.recent_hours} hours ({list.recent.length})
            </h3>
            {list.recent.length === 0 && (
              <p className="lab-bench-empty">No critical result was closed in that time.</p>
            )}
            {list.recent.map(n => (
              <CriticalCard key={n.id} n={n} now={now} canLog={false}
                roles={list.recipient_roles} methods={list.methods}
                onDone={onDone} onFailed={onFailed} />
            ))}
          </section>
        </>
      )}
    </div>
  )
}
