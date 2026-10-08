/**
 * Receive: the laboratory's front bench (lis-9).
 *
 * Scan (or type) the accession number on a tube that has arrived. The server
 * checks its check digit before it looks anything up, so a misread digit is
 * refused in words and never opens another patient's tube. The sample then
 * shows what it is, whose it is, how long it has been in transit and what can
 * be done with it now; this screen draws only the steps the server offers:
 *   * Receive a collected sample. One past its type's stability time is
 *     received only with a tick and a reason; rejecting it as delayed in
 *     transit is offered first.
 *   * Reject it with a reason from the laboratory's list. The answer says
 *     whether the ward was told in the patient's case room and, for a
 *     mislabelled or unlabelled sample with the NABH module on, asks for a
 *     specimen error to be reported.
 *   * For a rejected sample, the decision: recollect (a new tube appears on
 *     the ward's Samples list) or cancel its tests with a reason.
 *
 * Below the scan box: samples in transit (STAT first, then the longest in
 * transit) and rejected samples waiting for a decision. Each list read is
 * audited, so the lists load when the screen opens, after each step and when
 * asked; there is no polling.
 *
 * Receiving and rejecting finish open work, so this view stays while the
 * module drains; recollecting starts new work and is not offered then.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'

import { toLabError } from '../../../api/lab'
import {
  cancelRejectedTests,
  getReceivingLists,
  receiveSpecimen,
  recollectSpecimen,
  rejectSpecimen,
  scanSpecimen,
  type IncidentPrompt,
  type ReceiptSpecimen,
  type ReceivingEntry,
  type ReceivingLists,
  type RejectionReason,
  type WardNotice,
} from '../../../api/lab/receipt'
import { formatAccession, priorityText } from '../../../api/lab/specimens'
import { formatClinicalDateTime, formatClinicalTime } from '../../../lib/datetime'
import type { LabViewProps } from './viewRegistry'
import './LabBench.css'

export const viewId = 'receive'
export const viewLabel = 'Receive'
export const viewOrder = 20
export const viewPermission = 'lab.receive'
export const viewWhenDraining = 'show'

type Tone = 'ok' | 'warn' | 'error'

interface Notice {
  tone: Tone
  lines: string[]
}

/** "45 min", "2 h 5 min". */
function minutesText(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined) return ''
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours} h ${rest} min` : `${hours} h`
}

function wardNoticeText(notice: WardNotice | null | undefined): string {
  if (!notice) return ''
  if (notice.posted) return 'The ward was told in the patient\'s case room.'
  if (notice.reason === 'no_case_room') {
    return 'The ward could not be told in Relay: this patient has no case room. Phone the ward.'
  }
  if (notice.reason === 'case_room_closed') {
    return 'The ward could not be told in Relay: this patient has been discharged, so the case room is closed. Phone the ward.'
  }
  return 'The ward could not be told in Relay. Phone the ward.'
}

function failureText(err: unknown, fallback: string): string {
  const failure = toLabError(err)
  if (failure.isModuleDisabled) {
    return 'This step has stopped: the laboratory module is switched off on this site.'
  }
  if (failure.isNotFound) return 'No sample with this number was found.'
  return failure.message || fallback
}

function who(entry: { patient: { name: string; mrn: string | null } }): string {
  return `${entry.patient.name}${entry.patient.mrn ? `, UHID ${entry.patient.mrn}` : ''}`
}

function tubeText(entry: { container: string | null; specimen_display: string }): string {
  return [entry.specimen_display, entry.container].filter(Boolean).join(', ')
}

interface SpecimenCardProps {
  specimen: ReceiptSpecimen
  reasons: RejectionReason[]
  draining: boolean
  onReceived: (specimen: ReceiptSpecimen, message: string) => void
  onRejected: (specimen: ReceiptSpecimen, lines: string[], incident: IncidentPrompt | null) => void
  onDecided: (specimen: ReceiptSpecimen | null, message: string) => void
}

function SpecimenCard({ specimen, reasons, draining, onReceived, onRejected, onDecided }: SpecimenCardProps) {
  const { actions, stability } = specimen
  const suggested = stability.suggested_rejection && reasons.some(r => r.code === stability.suggested_rejection)
    ? stability.suggested_rejection
    : ''
  const [mode, setMode] = useState<'idle' | 'reject' | 'cancel'>('idle')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [acceptLate, setAcceptLate] = useState(false)
  const [lateReason, setLateReason] = useState('')
  const [reasonCode, setReasonCode] = useState(suggested)
  const [rejectNote, setRejectNote] = useState('')
  const [cancelReason, setCancelReason] = useState('')

  const noteNeeded = reasonCode === 'other'
  const canReject = reasonCode !== '' && (!noteNeeded || rejectNote.trim() !== '') && !busy
  const canReceive = !busy && (!stability.exceeded || (acceptLate && lateReason.trim() !== ''))

  const run = async (step: () => Promise<void>, fallback: string) => {
    setBusy(true)
    setError(null)
    try {
      await step()
    } catch (err) {
      setError(failureText(err, fallback))
    } finally {
      setBusy(false)
    }
  }

  const receive = () => run(async () => {
    const result = await receiveSpecimen(specimen.id, stability.exceeded
      ? { accept_past_stability: true, receipt_note: lateReason.trim() }
      : {})
    onReceived(result.specimen,
      `Sample ${formatAccession(specimen.id)} received: ${result.items_received} ${result.items_received === 1 ? 'test' : 'tests'} on the worklists.`)
  }, 'The sample could not be received.')

  const reject = (e: FormEvent) => {
    e.preventDefault()
    if (!canReject) return
    void run(async () => {
      const result = await rejectSpecimen(specimen.id, {
        reason_code: reasonCode,
        ...(rejectNote.trim() ? { note: rejectNote.trim() } : {}),
      })
      onRejected(result.specimen, [
        `Sample ${formatAccession(specimen.id)} rejected. Recollect it or cancel its tests below.`,
        wardNoticeText(result.ward_notice),
      ], result.incident_prompt)
    }, 'The sample could not be rejected.')
  }

  const recollect = () => run(async () => {
    const result = await recollectSpecimen(specimen.id)
    onDecided(result.specimen,
      `A new sample ${formatAccession(result.recollected_as)} is planned. The ward sees it under Samples to collect.`)
  }, 'The recollection could not be requested.')

  const cancel = (e: FormEvent) => {
    e.preventDefault()
    if (!cancelReason.trim() || busy) return
    void run(async () => {
      const result = await cancelRejectedTests(specimen.id, cancelReason.trim())
      onDecided(result.specimen,
        `${result.cancelled_items} rejected ${result.cancelled_items === 1 ? 'test' : 'tests'} cancelled.`)
    }, 'The tests could not be cancelled.')
  }

  const stat = specimen.priority === 'stat'

  return (
    <section
      className={`lab-bench-card${stat ? ' lab-bench-card--stat' : ''}`}
      aria-label={`Sample ${formatAccession(specimen.id)}`}
    >
      <div className="lab-bench-card-head">
        <span className="lab-bench-name">{specimen.patient.name}</span>
        <span className="lab-bench-num lab-bench-strong">{formatAccession(specimen.id)}</span>
      </div>
      <dl className="lab-bench-facts">
        <dt>UHID</dt>
        <dd>{specimen.patient.mrn || 'Not recorded'}</dd>
        <dt>Ward</dt>
        <dd>{[specimen.patient.ward, specimen.patient.bed ? `Bed ${specimen.patient.bed}` : null].filter(Boolean).join(', ') || 'Not recorded'}</dd>
        <dt>Sample</dt>
        <dd>
          {stat && <span className="lab-bench-stat">{priorityText(specimen.priority)} </span>}
          {tubeText(specimen)}
          {specimen.priority === 'urgent' ? ', urgent' : ''}
        </dd>
        <dt>Status</dt>
        <dd>{specimen.status_text}</dd>
        {specimen.collected_at && (
          <>
            <dt>Collected</dt>
            <dd>
              {formatClinicalDateTime(specimen.collected_at)}
              {specimen.collected_by_name ? ` by ${specimen.collected_by_name}` : ''}
            </dd>
          </>
        )}
        {specimen.transit_minutes !== null && (
          <>
            <dt>In transit</dt>
            <dd>{minutesText(specimen.transit_minutes)}</dd>
          </>
        )}
        {specimen.received_at && (
          <>
            <dt>Received</dt>
            <dd>
              {formatClinicalDateTime(specimen.received_at)}
              {specimen.received_by_name ? ` by ${specimen.received_by_name}` : ''}
            </dd>
          </>
        )}
        {specimen.rejected_at && (
          <>
            <dt>Rejected</dt>
            <dd>
              {[specimen.rejection_display, specimen.rejection_note].filter(Boolean).join(': ')}
              {`, ${formatClinicalDateTime(specimen.rejected_at)}`}
              {specimen.rejected_by_name ? ` by ${specimen.rejected_by_name}` : ''}
            </dd>
          </>
        )}
        {specimen.recollect_of && (
          <>
            <dt>Recollection of</dt>
            <dd className="lab-bench-num">{formatAccession(specimen.recollect_of)}</dd>
          </>
        )}
        {specimen.recollected_as.length > 0 && (
          <>
            <dt>Recollected as</dt>
            <dd className="lab-bench-num">{specimen.recollected_as.map(formatAccession).join(', ')}</dd>
          </>
        )}
      </dl>

      {specimen.tests.length > 0 && (
        <ul className="lab-bench-tests" aria-label="Tests on this sample">
          {specimen.tests.map(test => (
            <li key={test.item_id}>
              {test.name}: {test.status_text}
              {test.cancelled
                ? ` by ${test.cancelled.by_name ?? 'someone'}${test.cancelled.at ? ` at ${formatClinicalTime(test.cancelled.at)}` : ''}`
                : ''}
            </li>
          ))}
        </ul>
      )}

      {stability.exceeded && actions.receive && (
        <div className="lab-bench-note lab-bench-note--warn" role="status">
          <p className="lab-bench-note-title">Past its stability time</p>
          <p>
            Collected {minutesText(stability.age_minutes)} ago; this sample type is stable for{' '}
            {minutesText(stability.stability_minutes)}. Reject it as delayed in transit, or say
            why it is being processed.
          </p>
          <label className="lab-bench-check">
            <input type="checkbox" checked={acceptLate} onChange={e => setAcceptLate(e.target.checked)} />
            Process it anyway
          </label>
          {acceptLate && (
            <label className="lab-bench-field">
              Why it is being processed
              <input
                className="lab-bench-input"
                value={lateReason}
                onChange={e => setLateReason(e.target.value)}
                maxLength={500}
              />
            </label>
          )}
        </div>
      )}

      {mode === 'idle' && (actions.receive || actions.reject || actions.recollect || actions.cancel_rejected) && (
        <div className="lab-bench-actions">
          {actions.receive && (
            <button type="button" className="lab-bench-btn lab-bench-btn--primary" disabled={!canReceive}
              onClick={() => void receive()}>
              {busy ? 'Receiving…' : 'Receive'}
            </button>
          )}
          {actions.reject && (
            <button type="button" className="lab-bench-btn" disabled={busy}
              onClick={() => { setMode('reject'); setError(null) }}>
              Reject
            </button>
          )}
          {actions.recollect && !draining && (
            <button type="button" className="lab-bench-btn lab-bench-btn--primary" disabled={busy}
              onClick={() => void recollect()}>
              {busy ? 'Requesting…' : 'Recollect'}
            </button>
          )}
          {actions.cancel_rejected && (
            <button type="button" className="lab-bench-btn" disabled={busy}
              onClick={() => { setMode('cancel'); setError(null) }}>
              Cancel these tests
            </button>
          )}
        </div>
      )}

      {actions.recollect && draining && (
        <p className="lab-bench-note lab-bench-note--warn">
          New collections have stopped while the laboratory module is switched off. Cancel these
          tests instead, or ask the ward to reorder them once the module is back on.
        </p>
      )}

      {mode === 'reject' && (
        <form className="lab-bench-form" aria-label="Reject this sample" onSubmit={reject}>
          <label className="lab-bench-field">
            Reason
            <select className="lab-bench-input" value={reasonCode} onChange={e => setReasonCode(e.target.value)}>
              <option value="">Choose a reason</option>
              {reasons.map(r => <option key={r.code} value={r.code}>{r.display}</option>)}
            </select>
          </label>
          <label className="lab-bench-field">
            {noteNeeded ? 'What was wrong' : 'Note (optional)'}
            <input className="lab-bench-input" value={rejectNote} maxLength={500}
              onChange={e => setRejectNote(e.target.value)} />
          </label>
          <div className="lab-bench-actions">
            <button type="submit" className="lab-bench-btn lab-bench-btn--primary" disabled={!canReject}>
              {busy ? 'Rejecting…' : 'Reject sample'}
            </button>
            <button type="button" className="lab-bench-btn" onClick={() => setMode('idle')}>Back</button>
          </div>
        </form>
      )}

      {mode === 'cancel' && (
        <form className="lab-bench-form" aria-label="Cancel the rejected tests" onSubmit={cancel}>
          <label className="lab-bench-field">
            Why they are cancelled instead of recollected
            <input className="lab-bench-input" value={cancelReason} maxLength={500}
              onChange={e => setCancelReason(e.target.value)} />
          </label>
          <div className="lab-bench-actions">
            <button type="submit" className="lab-bench-btn lab-bench-btn--primary"
              disabled={!cancelReason.trim() || busy}>
              {busy ? 'Cancelling…' : 'Cancel tests'}
            </button>
            <button type="button" className="lab-bench-btn" onClick={() => setMode('idle')}>Back</button>
          </div>
        </form>
      )}

      {error && <p className="lab-bench-note lab-bench-note--error" role="alert">{error}</p>}
    </section>
  )
}

interface ListProps {
  entries: ReceivingEntry[]
  onOpen: (id: string) => void
}

function InTransitTable({ entries, onOpen }: ListProps) {
  return (
    <div className="lab-bench-table-wrap">
      <table className="lab-bench-table">
        <thead>
          <tr>
            <th scope="col">Sample</th>
            <th scope="col">Patient</th>
            <th scope="col">Tube</th>
            <th scope="col">Tests</th>
            <th scope="col">Collected</th>
            <th scope="col">In transit</th>
            <th scope="col"><span className="lab-bench-quiet">Open</span></th>
          </tr>
        </thead>
        <tbody>
          {entries.map(entry => (
            <tr key={entry.id}>
              <td className="lab-bench-num">
                {entry.priority === 'stat' && <span className="lab-bench-stat">STAT </span>}
                {formatAccession(entry.id)}
              </td>
              <td>{who(entry)}</td>
              <td>{tubeText(entry)}</td>
              <td>{entry.tests.map(t => t.short_name).join(', ')}</td>
              <td className="lab-bench-num">
                {formatClinicalTime(entry.collected_at)}
                {entry.collected_by_name ? <span className="lab-bench-quiet"> by {entry.collected_by_name}</span> : null}
              </td>
              <td className="lab-bench-num">
                {minutesText(entry.transit_minutes)}
                {entry.stability.exceeded ? <span className="lab-bench-flag lab-bench-flag--out">, past stability</span> : null}
              </td>
              <td>
                <button type="button" className="lab-bench-btn" onClick={() => onOpen(entry.id)}>Open</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function RejectedTable({ entries, onOpen }: ListProps) {
  return (
    <div className="lab-bench-table-wrap">
      <table className="lab-bench-table">
        <thead>
          <tr>
            <th scope="col">Sample</th>
            <th scope="col">Patient</th>
            <th scope="col">Tests</th>
            <th scope="col">Reason</th>
            <th scope="col">Rejected</th>
            <th scope="col"><span className="lab-bench-quiet">Decide</span></th>
          </tr>
        </thead>
        <tbody>
          {entries.map(entry => (
            <tr key={entry.id}>
              <td className="lab-bench-num">
                {entry.priority === 'stat' && <span className="lab-bench-stat">STAT </span>}
                {formatAccession(entry.id)}
              </td>
              <td>{who(entry)}</td>
              <td>{entry.tests.map(t => t.short_name).join(', ')}</td>
              <td>{[entry.rejection_display ?? entry.rejection_code?.replace(/_/g, ' '), entry.rejection_note].filter(Boolean).join(': ')}</td>
              <td className="lab-bench-num">
                {formatClinicalDateTime(entry.rejected_at)}
                {entry.rejected_by_name ? <span className="lab-bench-quiet"> by {entry.rejected_by_name}</span> : null}
              </td>
              <td>
                <button type="button" className="lab-bench-btn" onClick={() => onOpen(entry.id)}>Open</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function ReceiveView({ state, onModuleOff }: LabViewProps) {
  const draining = state === 'draining'
  const [lists, setLists] = useState<ReceivingLists | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [code, setCode] = useState('')
  const [scanning, setScanning] = useState(false)
  const [scanError, setScanError] = useState<string | null>(null)
  const [specimen, setSpecimen] = useState<ReceiptSpecimen | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [incident, setIncident] = useState<IncidentPrompt | null>(null)
  const scanBox = useRef<HTMLInputElement>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setLists(await getReceivingLists())
      setListError(null)
    } catch (err) {
      const failure = toLabError(err)
      if (failure.isModuleDisabled) onModuleOff()
      else setListError(failure.message || 'The receiving lists could not be loaded.')
    } finally {
      setLoading(false)
    }
  }, [onModuleOff])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void load() }, [load])

  // A keyboard-mode scanner types into whatever has focus: start in the box.
  useEffect(() => { scanBox.current?.focus() }, [])

  const open = useCallback(async (typed: string) => {
    const text = typed.trim()
    if (!text) return
    setScanning(true)
    setScanError(null)
    setIncident(null)
    try {
      const found = await scanSpecimen(text)
      setSpecimen(found.specimen)
      setNotice(null)
    } catch (err) {
      setSpecimen(null)
      setScanError(failureText(err, 'The sample could not be found.'))
    } finally {
      setScanning(false)
    }
  }, [])

  const onScan = (e: FormEvent) => {
    e.preventDefault()
    void open(code)
  }

  const nextTube = () => {
    setCode('')
    scanBox.current?.focus()
  }

  const reasons = useMemo(() => lists?.rejection_reasons ?? [], [lists])

  return (
    <div className="lab-bench">
      <div className="lab-bench-head">
        <div>
          <h2 className="lab-bench-title">Receive samples</h2>
          <p className="lab-bench-sub">
            Scan the label, or type the 12-digit number. A misread digit is refused, never looked up.
          </p>
        </div>
        <button type="button" className="lab-bench-btn" onClick={() => void load()} disabled={loading}>
          {loading ? 'Refreshing…' : 'Refresh lists'}
        </button>
      </div>

      <form className="lab-bench-form" onSubmit={onScan} aria-label="Scan a sample">
        <label className="lab-bench-field">
          Accession number
          <input
            ref={scanBox}
            className="lab-bench-input lab-bench-input--scan"
            value={code}
            onChange={e => setCode(e.target.value)}
            inputMode="numeric"
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        <button type="submit" className="lab-bench-btn lab-bench-btn--primary" disabled={scanning || !code.trim()}>
          {scanning ? 'Looking up…' : 'Find sample'}
        </button>
      </form>

      {scanError && <p className="lab-bench-note lab-bench-note--error" role="alert">{scanError}</p>}

      {notice && (
        <div className={`lab-bench-note lab-bench-note--${notice.tone}`} role="status">
          {notice.lines.filter(Boolean).map(line => <p key={line}>{line}</p>)}
        </div>
      )}

      {incident && (
        <div className="lab-bench-note lab-bench-note--warn" role="status">
          <p className="lab-bench-note-title">Report a specimen error</p>
          <p>{incident.message}</p>
        </div>
      )}

      {specimen && (
        <SpecimenCard
          key={`${specimen.id}:${specimen.status}`}
          specimen={specimen}
          reasons={reasons}
          draining={draining}
          onReceived={(next, message) => {
            setSpecimen(next)
            setNotice({ tone: 'ok', lines: [message] })
            void load()
            nextTube()
          }}
          onRejected={(next, lines, prompt) => {
            setSpecimen(next)
            setNotice({ tone: lines.some(l => l.includes('Phone the ward')) ? 'warn' : 'ok', lines })
            setIncident(prompt)
            void load()
          }}
          onDecided={(next, message) => {
            setSpecimen(next)
            setNotice({ tone: 'ok', lines: [message] })
            void load()
            nextTube()
          }}
        />
      )}

      {listError && (
        <div className="lab-bench-note lab-bench-note--error" role="alert">
          <p>Couldn&apos;t load the receiving lists. {listError}</p>
          <p>This is a display failure, not an empty list.</p>
        </div>
      )}

      {lists && !listError && (
        <>
          <section className="lab-bench-section" aria-labelledby="lab-receive-transit">
            <h3 id="lab-receive-transit" className="lab-bench-section-title">
              In transit ({lists.in_transit.length})
            </h3>
            {lists.in_transit.length === 0
              ? <p className="lab-bench-empty">No collected sample is waiting to be received.</p>
              : <InTransitTable entries={lists.in_transit} onOpen={id => void open(id)} />}
          </section>
          <section className="lab-bench-section" aria-labelledby="lab-receive-rejected">
            <h3 id="lab-receive-rejected" className="lab-bench-section-title">
              Rejected, waiting for a decision ({lists.rejected.length})
            </h3>
            {lists.rejected.length === 0
              ? <p className="lab-bench-empty">No rejected sample is waiting for a decision.</p>
              : <RejectedTable entries={lists.rejected} onOpen={id => void open(id)} />}
          </section>
        </>
      )}
    </div>
  )
}
