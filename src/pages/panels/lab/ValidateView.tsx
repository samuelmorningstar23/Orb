/**
 * Validate: technical validation and authorisation (lis-11, lis-12).
 *
 * TWO QUEUES, each grouped by sample, STAT first then the oldest receipt, in
 * the order the server sends:
 *   1. Waiting for technical validation (lab.result): results entered on the
 *      bench. Tick the ones checked and validate them together. A delta flag
 *      (a change beyond the test's delta limit from the patient's earlier
 *      result, ruling LIS-R9 (a)) needs the technologist's reason first, so
 *      its row cannot be ticked until one is given.
 *   2. Waiting for authorisation (lab.authorise): validated results. A
 *      signatory authorises one sample at a time, and authorising releases
 *      the results to the patient's chart. Partial panels release analyte by
 *      analyte (LIS-R16 (a)). A signatory who disagrees sends a result back
 *      to the bench with a reason.
 *
 * TWO DIFFERENT PEOPLE (ruling LIS-R5, held; the shipped default). While the
 * site requires it, whoever entered, changed, explained the delta of or
 * validated a result cannot authorise it: its row says so and cannot be
 * ticked. The server decides again on every call; this screen only draws
 * what it said.
 *
 * CRITICAL RESULTS. A result flagged critically high or low (against a
 * critical limit a pathologist approved, ruling LIS-R1) is said in words on
 * its sample, with the instruction to phone the ward. Technical validation
 * raises it (lis-15, ruling LIS-R4 (a): preliminary, not yet authorised) and
 * Orb posts it to the patient's case room; the step's notice says whether
 * that post was delivered, and the call is logged on Critical results.
 *
 * REPORTED RESULTS (lis-13). Below the queues, a signatory finds a reported
 * sample by its accession number to amend a result (two signatories while
 * the site requires two different people) or withdraw one entered in error
 * (./ReportedResults).
 *
 * The queue read is audited, so it loads when the view opens, after each
 * step and when asked; there is no polling. Validating and releasing finish
 * open work, so this view stays while the module drains.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'

import { toLabError } from '../../../api/lab'
import { criticalsOf, deliveryWords } from '../../../api/lab/criticals'
import { formatAccession, priorityText } from '../../../api/lab/specimens'
import {
  acknowledgeDelta,
  authoriseAndRelease,
  getValidationQueues,
  returnToBench,
  seenOfQueue,
  techValidate,
  type QueueGroup,
  type QueueResult,
  type ValidationQueues,
} from '../../../api/lab/validation'
import { formatClinicalDateTime, formatClinicalTime } from '../../../lib/datetime'
import ReportedResults from './ReportedResults'
import type { LabViewProps } from './viewRegistry'
import './LabBench.css'

export const viewId = 'validate'
export const viewLabel = 'Validate'
export const viewOrder = 50
export const viewPermission = 'lab.result'
export const viewWhenDraining = 'show'

/** Refusals that mean the screen is out of date: reload the queue. */
const STALE_CODES = new Set([
  'result_changed', 'not_live', 'already_validated', 'already_released', 'not_validated',
  'item_cancelled', 'specimen_rejected', 'delta_reason_required', 'already_acknowledged',
  'second_person_required', 'not_found', 'specimen_not_on_bench',
])

function count(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

function isOut(result: QueueResult): boolean {
  return ['H', 'L', 'HH', 'LL', 'A'].includes(result.abnormal_flag)
}

function valueText(result: QueueResult): string {
  const value = result.value_text ?? ''
  return result.unit ? `${value} ${result.unit}` : value
}

function groupWhere(group: QueueGroup): string {
  const where = [group.patient.ward, group.patient.bed ? `Bed ${group.patient.bed}` : null]
    .filter(Boolean).join(', ')
  return [
    `${group.patient.name}`,
    group.patient.mrn ? `UHID ${group.patient.mrn}` : null,
    where || null,
    group.collected_at ? `Collected ${formatClinicalDateTime(group.collected_at)}` : null,
  ].filter(Boolean).join(' · ')
}

function GroupHeading({ group }: { group: QueueGroup }) {
  return (
    <p className="lab-bench-section-title">
      {group.priority !== 'routine' && (
        <span className="lab-bench-stat">{priorityText(group.priority)} </span>
      )}
      <span className="lab-bench-num">{formatAccession(group.specimen_id)}</span>
      <span className="lab-bench-quiet"> · {groupWhere(group)}</span>
    </p>
  )
}

/** The delta check of one result in words, and the reason box a flag needs. */
function DeltaCell({ result, canExplain, onExplained, onFailed }: {
  result: QueueResult
  canExplain: boolean
  onExplained: (message: string) => void
  onFailed: (message: string, stale: boolean) => void
}) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const delta = result.delta
  if (!delta || delta.status === 'no_previous') {
    return <span className="lab-bench-quiet">{delta ? 'No earlier result' : 'No delta check'}</span>
  }
  const flagged = delta.status === 'flagged'
  const label = `${result.test.name}`

  const save = async () => {
    if (busy) return
    if (!reason.trim()) {
      setError('Say why this change from the earlier result is expected, or what was checked.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const out = await acknowledgeDelta(result.result_id, reason.trim(), seenOfQueue(result))
      onExplained(out.acknowledged
        ? `${result.test.name}: delta reason saved.`
        : `${result.test.name}: ${out.message ?? 'no delta reason is needed now.'}`)
    } catch (err) {
      const failure = toLabError(err)
      setError(failure.message || 'The reason could not be saved.')
      onFailed(`${result.test.name}: ${failure.message}`, STALE_CODES.has(failure.code))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <p className={flagged ? 'lab-bench-flag lab-bench-flag--out' : 'lab-bench-quiet'}>
        {flagged ? 'Delta flag. ' : ''}{delta.note}
        {delta.previous?.observed_at
          ? ` Earlier result ${formatClinicalDateTime(delta.previous.observed_at)}.`
          : ''}
      </p>
      {flagged && result.delta_ack && (
        <p>
          {result.needs_delta_reason ? 'The earlier reason' : 'Reason'}
          {result.delta_ack.by_name ? ` from ${result.delta_ack.by_name}` : ''}
          {result.needs_delta_reason ? ' no longer covers this check' : ''}
          : {result.delta_ack.reason}
        </p>
      )}
      {flagged && result.needs_delta_reason && canExplain && (
        <div className="lab-bench-actions">
          <input
            className="lab-bench-input"
            aria-label={`Delta reason, ${label}`}
            placeholder="Why the change is expected, or what was checked"
            value={reason}
            maxLength={500}
            onChange={e => setReason(e.target.value)}
          />
          <button type="button" className="lab-bench-btn" disabled={busy} onClick={() => void save()}>
            {busy ? 'Saving…' : 'Save reason'}
          </button>
        </div>
      )}
      {error && <p className="lab-bench-error" role="alert">{error}</p>}
    </div>
  )
}

interface StepProps {
  group: QueueGroup
  onDone: (message: string) => void
  onFailed: (message: string, stale: boolean) => void
}

/** A critical result on a sample, in words, with what to do. Validating it
 *  posts it to the patient's case room as preliminary (lis-15, ruling LIS-R4
 *  (a)); the laboratory still phones the ward and logs the call with a
 *  read-back on Critical results, which is the only thing that closes it. */
function CriticalNotice({ results, preliminary }: { results: QueueResult[]; preliminary: boolean }) {
  if (results.length === 0) return null
  const limits = results.map(r => r.critical_limit_text).filter(Boolean)
  return (
    <div className="lab-bench-note lab-bench-note--error" role="alert">
      <p className="lab-bench-note-title">
        Critical result: {results.map(r => `${r.test.name} ${valueText(r)}`).join(', ')}
      </p>
      <p>
        {limits.length > 0 ? `${limits.join('; ')}. ` : ''}
        {preliminary
          ? 'Validating it posts it to the patient\'s case room as preliminary. Phone the ward now, say the value is preliminary and not yet authorised, and log the call with the read-back on Critical results.'
          : 'Phone the ward now and log the call with the read-back on Critical results.'}
      </p>
    </div>
  )
}

/** One sample waiting for technical validation. */
function TechnicalGroup({ group, onDone, onFailed }: StepProps) {
  const ready = useMemo(
    () => group.results.filter(r => !r.needs_delta_reason).map(r => r.result_id),
    [group.results])
  const [chosen, setChosen] = useState<Set<number>>(() => new Set(ready))
  const [busy, setBusy] = useState(false)
  const picked = group.results.filter(r => chosen.has(r.result_id) && !r.needs_delta_reason)
  const waitingForReason = group.results.filter(r => r.needs_delta_reason).length
  const criticals = group.results.filter(r => r.is_critical)

  const toggle = (id: number) => setChosen(current => {
    const next = new Set(current)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })

  const validate = async () => {
    if (busy || picked.length === 0) return
    setBusy(true)
    try {
      const out = await techValidate(picked.map(r => ({ result_id: r.result_id, expected: seenOfQueue(r) })))
      const told = deliveryWords(criticalsOf(out))
      onDone(`${count(out.validated, 'result')} technically validated on ${formatAccession(group.specimen_id)}.`
        + (told ? ` ${told}` : ''))
    } catch (err) {
      const failure = toLabError(err)
      onFailed(failure.message || 'Nothing was validated.', STALE_CODES.has(failure.code))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="lab-bench-section">
      <GroupHeading group={group} />
      <CriticalNotice results={criticals} preliminary />
      <div className="lab-bench-table-wrap">
        <table className="lab-bench-table" aria-label={`Technical validation, ${formatAccession(group.specimen_id)}`}>
          <thead>
            <tr>
              <th scope="col"><span className="lab-bench-quiet">Include</span></th>
              <th scope="col">Test</th>
              <th scope="col">Result</th>
              <th scope="col">Flag</th>
              <th scope="col">Reference range</th>
              <th scope="col">Delta check</th>
              <th scope="col">Entered by</th>
            </tr>
          </thead>
          <tbody>
            {group.results.map(r => (
              <tr key={r.result_id}>
                <td>
                  <label className="lab-bench-check">
                    <input
                      type="checkbox"
                      aria-label={`Include ${r.test.name}`}
                      checked={chosen.has(r.result_id) && !r.needs_delta_reason}
                      disabled={r.needs_delta_reason}
                      onChange={() => toggle(r.result_id)}
                    />
                  </label>
                </td>
                <td>
                  <span className="lab-bench-strong">{r.test.name}</span>
                  {r.returned && (
                    <p className="lab-bench-quiet">
                      Sent back{r.returned.by_name ? ` by ${r.returned.by_name}` : ''}
                      {r.returned.reason ? `: ${r.returned.reason}` : ''}
                    </p>
                  )}
                </td>
                <td className="lab-bench-num">{valueText(r)}</td>
                <td><span className={isOut(r) ? 'lab-bench-flag lab-bench-flag--out' : 'lab-bench-quiet'}>{r.flag_text}</span></td>
                <td className="lab-bench-quiet">{r.reference_range_text}</td>
                <td><DeltaCell result={r} canExplain onExplained={onDone} onFailed={onFailed} /></td>
                <td className="lab-bench-quiet">
                  {r.entered_by_name ?? ''}{r.entered_at ? `, ${formatClinicalTime(r.entered_at)}` : ''}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="lab-bench-actions">
        <button
          type="button"
          className="lab-bench-btn lab-bench-btn--primary"
          disabled={busy || picked.length === 0}
          onClick={() => void validate()}
        >
          {busy ? 'Validating…' : `Validate ${count(picked.length, 'result')}`}
        </button>
        {waitingForReason > 0 && (
          <span className="lab-bench-quiet">
            {count(waitingForReason, 'delta flag')} {waitingForReason === 1 ? 'needs' : 'need'} a reason before validation.
          </span>
        )}
      </div>
    </div>
  )
}

/** The send-back box for one validated result. */
function SendBack({ result, onDone, onFailed }: {
  result: QueueResult
  onDone: (message: string) => void
  onFailed: (message: string, stale: boolean) => void
}) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (!open) {
    return (
      <button type="button" className="lab-bench-btn" onClick={() => setOpen(true)}>
        Send back
      </button>
    )
  }

  const send = async () => {
    if (busy) return
    if (!reason.trim()) {
      setError('Say why the result goes back to the bench.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await returnToBench(result.result_id, reason.trim(), seenOfQueue(result))
      onDone(`${result.test.name} sent back to the bench.`)
    } catch (err) {
      const failure = toLabError(err)
      setError(failure.message || 'The result could not be sent back.')
      onFailed(`${result.test.name}: ${failure.message}`, STALE_CODES.has(failure.code))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="lab-bench-form">
      <input
        className="lab-bench-input"
        aria-label={`Why ${result.test.name} goes back`}
        placeholder="Why it goes back to the bench"
        value={reason}
        maxLength={500}
        onChange={e => setReason(e.target.value)}
      />
      <button type="button" className="lab-bench-btn" disabled={busy} onClick={() => void send()}>
        {busy ? 'Sending…' : 'Send back to the bench'}
      </button>
      <button type="button" className="lab-bench-btn" disabled={busy} onClick={() => { setOpen(false); setError(null) }}>
        Keep it
      </button>
      {error && <p className="lab-bench-error" role="alert">{error}</p>}
    </div>
  )
}

/** One sample waiting for a signatory. */
function AuthorisationGroup({ group, canAuthorise, distinct, onDone, onFailed }: StepProps & {
  canAuthorise: boolean
  distinct: boolean
}) {
  const allowed = useMemo(
    () => group.results.filter(r => r.may_authorise).map(r => r.result_id),
    [group.results])
  const [chosen, setChosen] = useState<Set<number>>(() => new Set(allowed))
  const [busy, setBusy] = useState(false)
  const picked = group.results.filter(r => chosen.has(r.result_id) && r.may_authorise)
  const tookPart = group.results.filter(r => r.you_took_part).length
  const criticals = group.results.filter(r => r.is_critical)

  const toggle = (id: number) => setChosen(current => {
    const next = new Set(current)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })

  const release = async () => {
    if (busy || picked.length === 0) return
    setBusy(true)
    try {
      const out = await authoriseAndRelease(
        group.specimen_id,
        picked.map(r => ({ result_id: r.result_id, expected: seenOfQueue(r) })))
      const conflict = out.orders.some(o => o.conflict)
      const told = deliveryWords(criticalsOf(out))
      onDone(`${count(out.released, 'result')} released to the chart for ${group.patient.name}.`
        + (out.one_person ? ` Recorded as one person doing every step for ${count(out.one_person, 'result')}.` : '')
        + (conflict ? ' The order had already been changed outside the laboratory; it is on the exceptions list.' : '')
        + (told ? ` ${told}` : ''))
    } catch (err) {
      const failure = toLabError(err)
      onFailed(failure.message || 'Nothing was released.', STALE_CODES.has(failure.code))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="lab-bench-section">
      <GroupHeading group={group} />
      <CriticalNotice results={criticals} preliminary={false} />
      <div className="lab-bench-table-wrap">
        <table className="lab-bench-table" aria-label={`Authorisation, ${formatAccession(group.specimen_id)}`}>
          <thead>
            <tr>
              <th scope="col"><span className="lab-bench-quiet">Include</span></th>
              <th scope="col">Test</th>
              <th scope="col">Result</th>
              <th scope="col">Flag</th>
              <th scope="col">Reference range</th>
              <th scope="col">Delta check</th>
              <th scope="col">Entered and validated by</th>
              {canAuthorise && <th scope="col"><span className="lab-bench-quiet">Send back</span></th>}
            </tr>
          </thead>
          <tbody>
            {group.results.map(r => (
              <tr key={r.result_id}>
                <td>
                  {canAuthorise && (
                    <label className="lab-bench-check">
                      <input
                        type="checkbox"
                        aria-label={`Authorise ${r.test.name}`}
                        checked={chosen.has(r.result_id) && !!r.may_authorise}
                        disabled={!r.may_authorise}
                        onChange={() => toggle(r.result_id)}
                      />
                    </label>
                  )}
                </td>
                <td>
                  <span className="lab-bench-strong">{r.test.name}</span>
                  {canAuthorise && r.you_took_part && distinct && (
                    <p className="lab-bench-quiet">You took part in this result. Another signatory must authorise it.</p>
                  )}
                </td>
                <td className="lab-bench-num">{valueText(r)}</td>
                <td><span className={isOut(r) ? 'lab-bench-flag lab-bench-flag--out' : 'lab-bench-quiet'}>{r.flag_text}</span></td>
                <td className="lab-bench-quiet">{r.reference_range_text}</td>
                <td><DeltaCell result={r} canExplain={false} onExplained={onDone} onFailed={onFailed} /></td>
                <td className="lab-bench-quiet">
                  {r.entered_by_name ? `Entered by ${r.entered_by_name}` : ''}
                  {r.tech_validated_by_name ? `; validated by ${r.tech_validated_by_name}` : ''}
                  {r.tech_validated_at ? ` at ${formatClinicalTime(r.tech_validated_at)}` : ''}
                </td>
                {canAuthorise && (
                  <td><SendBack result={r} onDone={onDone} onFailed={onFailed} /></td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {canAuthorise ? (
        <div className="lab-bench-actions">
          <button
            type="button"
            className="lab-bench-btn lab-bench-btn--primary"
            disabled={busy || picked.length === 0}
            onClick={() => void release()}
          >
            {busy ? 'Releasing…' : `Authorise and release ${count(picked.length, 'result')}`}
          </button>
          {distinct && tookPart > 0 && (
            <span className="lab-bench-quiet">
              You took part in {count(tookPart, 'result')} on this sample; another signatory authorises {tookPart === 1 ? 'it' : 'them'}.
            </span>
          )}
        </div>
      ) : (
        <p className="lab-bench-quiet">A signatory authorises these results.</p>
      )}
    </div>
  )
}

export default function ValidateView({ onModuleOff }: LabViewProps) {
  const [queues, setQueues] = useState<ValidationQueues | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [warning, setWarning] = useState<string | null>(null)
  const [checkedAt, setCheckedAt] = useState<Date | null>(null)
  const [generation, setGeneration] = useState(0)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setQueues(await getValidationQueues())
      setError(null)
      setCheckedAt(new Date())
      // Each reload draws every sample again from the server's copy.
      setGeneration(n => n + 1)
    } catch (err) {
      const failure = toLabError(err)
      if (failure.isModuleDisabled) onModuleOff()
      else setError(failure.message || 'The validation lists could not be loaded.')
    } finally {
      setLoading(false)
    }
  }, [onModuleOff])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void load() }, [load])

  const onDone = (message: string) => {
    setWarning(null)
    setNotice(message)
    void load()
  }

  const onFailed = (message: string, stale: boolean) => {
    setNotice(null)
    setWarning(stale ? `${message} The lists have been reloaded.` : message)
    if (stale) void load()
  }

  const technicalCount = queues?.technical.reduce((n, g) => n + g.results.length, 0) ?? 0
  const waitingCount = queues?.authorisation.reduce((n, g) => n + g.results.length, 0) ?? 0

  return (
    <div className="lab-bench">
      <div className="lab-bench-head">
        <div>
          <h2 className="lab-bench-title">Validate and authorise</h2>
          <p className="lab-bench-sub">
            Every result is technically validated by a person and authorised by a signatory
            before it reaches the patient&apos;s chart.
            {queues ? (queues.require_distinct_authoriser
              ? ' This site requires two different people: whoever entered, changed or validated a result cannot authorise it.'
              : ' This site lets one signatory do every step; each release records it.') : ''}
            {checkedAt ? ` Checked at ${formatClinicalTime(checkedAt)}.` : ''}
          </p>
        </div>
        <button type="button" className="lab-bench-btn" onClick={() => void load()} disabled={loading}>
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {notice && <p className="lab-bench-note lab-bench-note--ok" role="status">{notice}</p>}
      {warning && <p className="lab-bench-note lab-bench-note--warn" role="status">{warning}</p>}

      {error && (
        <div className="lab-bench-note lab-bench-note--error" role="alert">
          <p>Couldn&apos;t load the validation lists. {error}</p>
          <p>This is a display failure, not an empty list.</p>
        </div>
      )}

      {queues && !error && (
        <>
          <section className="lab-bench-section" aria-labelledby="lab-validate-technical">
            <h3 id="lab-validate-technical" className="lab-bench-section-title">
              Waiting for technical validation ({technicalCount})
            </h3>
            {queues.technical.length === 0 && (
              <p className="lab-bench-empty">No entered result is waiting for technical validation.</p>
            )}
            {queues.technical.map(group => (
              <TechnicalGroup key={`${group.specimen_id}:${generation}`} group={group}
                onDone={onDone} onFailed={onFailed} />
            ))}
          </section>

          <section className="lab-bench-section" aria-labelledby="lab-validate-sign">
            <h3 id="lab-validate-sign" className="lab-bench-section-title">
              Waiting for authorisation ({waitingCount})
            </h3>
            {queues.authorisation.length === 0 && (
              <p className="lab-bench-empty">No validated result is waiting for a signatory.</p>
            )}
            {queues.authorisation.map(group => (
              <AuthorisationGroup key={`${group.specimen_id}:${generation}`} group={group}
                canAuthorise={queues.can_authorise} distinct={queues.require_distinct_authoriser}
                onDone={onDone} onFailed={onFailed} />
            ))}
          </section>

          <ReportedResults onModuleOff={onModuleOff} />
        </>
      )}
    </div>
  )
}
