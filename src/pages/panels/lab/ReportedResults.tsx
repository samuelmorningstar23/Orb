/**
 * Amend or withdraw a reported result (lis-13), on the Validate screen.
 *
 * A REPORTED RESULT IS NEVER CHANGED IN PLACE. Find the sample by its
 * accession number; each reported test shows the version on the chart and
 * the versions it replaced. A signatory may:
 *   · AMEND it with a reason. While the site requires two different people
 *     (ruling LIS-R5, the shipped default) this proposes the amendment, and
 *     a different signatory confirms it here; the chart keeps the reported
 *     value until then. With the setting off it is applied at once.
 *   · WITHDRAW a result entered in error, with a reason, in one step. A
 *     wrong-patient withdrawal prompts a NABH incident where that module is
 *     on.
 * Either tells the ward in the patient's case room once it is saved, and the
 * answer says whether it did. An amendment past a critical limit raises a
 * critical result (Critical results).
 *
 * The read is audited, so it loads only when asked. The server decides every
 * step again; this panel draws only what it said.
 */
import { useCallback, useState } from 'react'

import { toLabError } from '../../../api/lab'
import {
  amendResult,
  cancelAmendment,
  confirmAmendment,
  getReportedResults,
  noticeWords,
  seenOfReported,
  withdrawResult,
  type ReportedResults as ReportedResultsData,
  type ReportedTest,
  type ReportedVersion,
} from '../../../api/lab/amendments'
import { criticalsOf, deliveryWords } from '../../../api/lab/criticals'
import { formatAccession } from '../../../api/lab/specimens'
import { formatClinicalDateTime } from '../../../lib/datetime'

/** Refusals that mean the panel is out of date: read the sample again. */
const STALE_CODES = new Set([
  'result_changed', 'already_amended', 'withdrawn', 'not_live', 'amendment_pending',
  'no_pending_amendment', 'amendment_changed', 'not_found',
])

function valueWords(v: { value_text: string | null; unit: string | null }): string {
  const value = v.value_text ?? ''
  return v.unit ? `${value} ${v.unit}` : value
}

interface RowProps {
  test: ReportedTest
  canAmend: boolean
  distinct: boolean
  onDone: (message: string) => void
  onFailed: (message: string, stale: boolean) => void
}

function VersionLine({ v, what }: { v: ReportedVersion; what: string }) {
  return (
    <li>
      Version {v.version}: {valueWords(v)} ({v.flag_text}), {what}
      {v.released_by_name ? ` by ${v.released_by_name}` : ''}
      {v.released_at ? `, ${formatClinicalDateTime(v.released_at)}` : ''}
      {v.amend_reason ? `. Reason: ${v.amend_reason}` : ''}
    </li>
  )
}

function ReportedRow({ test, canAmend, distinct, onDone, onFailed }: RowProps) {
  const [mode, setMode] = useState<'none' | 'amend' | 'withdraw' | 'cancel'>('none')
  const [value, setValue] = useState('')
  const [comment, setComment] = useState('')
  const [reason, setReason] = useState('')
  const [deltaNote, setDeltaNote] = useState<string | null>(null)
  const [deltaReason, setDeltaReason] = useState('')
  const [wrongPatient, setWrongPatient] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const current = test.current
  const pending = test.pending_amendment
  const name = test.test.name

  const reset = () => {
    setMode('none')
    setValue('')
    setComment('')
    setReason('')
    setDeltaNote(null)
    setDeltaReason('')
    setWrongPatient(false)
    setError(null)
  }

  const run = async (call: () => Promise<string>) => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const message = await call()
      reset()
      onDone(message)
    } catch (err) {
      const failure = toLabError(err)
      if (failure.code === 'delta_reason_required') {
        const detail = (failure.detail ?? {}) as { delta?: { note?: string } }
        setDeltaNote(detail.delta?.note ?? failure.message)
        setError('This value is delta-flagged. Give a reason for the delta flag with the amendment.')
      } else {
        setError(failure.message || 'Nothing was changed.')
      }
      onFailed(`${name}: ${failure.message}`, STALE_CODES.has(failure.code))
    } finally {
      setBusy(false)
    }
  }

  const amend = () => run(async () => {
    if (!current) return ''
    const out = await amendResult(current.result_id, {
      value: value.trim(), reason: reason.trim(), comment: comment.trim() || null,
      delta_reason: deltaReason.trim() || null,
    }, seenOfReported(current))
    if (out.proposed) {
      return `${name}: amendment proposed. A second signatory must confirm it before the chart changes.`
    }
    return [`${name} amended.`, noticeWords(out.ward_notice), deliveryWords(criticalsOf(out))]
      .filter(Boolean).join(' ')
  })

  const applyProposal = () => run(async () => {
    if (!current || !pending) return ''
    const out = await confirmAmendment(current.result_id, pending, seenOfReported(current))
    return [`${name} amended.`, noticeWords(out.ward_notice), deliveryWords(criticalsOf(out))]
      .filter(Boolean).join(' ')
  })

  const cancel = () => run(async () => {
    if (!current || !pending) return ''
    await cancelAmendment(current.result_id, pending.proposal_id, reason.trim())
    return `${name}: the proposed amendment was cancelled. The reported result is unchanged.`
  })

  const withdraw = () => run(async () => {
    if (!current) return ''
    const out = await withdrawResult(current.result_id, reason.trim(), wrongPatient, seenOfReported(current))
    return [
      `${name} withdrawn: it is no longer on the chart.`,
      noticeWords(out.ward_notice),
      out.order.conflict ? 'It was the order\'s only reported test, so the order still shows resulted; it is on the exceptions list.' : '',
      out.open_criticals > 0 ? 'A critical result raised for it is still open: phone the ward and log the call on Critical results.' : '',
      out.incident_prompt ? out.incident_prompt.message : '',
    ].filter(Boolean).join(' ')
  })

  return (
    <div className="lab-bench-card">
      <div className="lab-bench-card-head">
        <span className="lab-bench-name">{name}</span>
        {current && (
          <span className="lab-bench-num">
            {valueWords(current)} <span className="lab-bench-quiet">({current.flag_text})</span>
          </span>
        )}
      </div>
      {current && (
        <p className="lab-bench-quiet">
          On the chart: version {current.version}
          {current.released_by_name ? `, reported by ${current.released_by_name}` : ''}
          {current.released_at ? ` at ${formatClinicalDateTime(current.released_at)}` : ''}
          {current.amend_reason ? `. Amended: ${current.amend_reason}` : ''}
        </p>
      )}
      {test.withdrawn && (
        <p className="lab-bench-flag lab-bench-flag--out">
          Withdrawn: {valueWords(test.withdrawn)} is no longer on the chart.
          {test.withdrawn.amend_reason ? ` Reason: ${test.withdrawn.amend_reason}` : ''}
        </p>
      )}
      {test.history.length > 0 && (
        <ul className="lab-bench-tests" aria-label={`Earlier versions, ${name}`}>
          {test.history.map(v => <VersionLine key={v.result_id} v={v} what="replaced" />)}
        </ul>
      )}

      {pending && (
        <div className="lab-bench-note lab-bench-note--warn">
          <p className="lab-bench-note-title">
            Amendment waiting for a second signatory: {valueWords(pending)}
            {pending.flag_text ? ` (${pending.flag_text})` : ''}
          </p>
          <p>
            Reason: {pending.reason}
            {pending.proposed_by_name ? `. Proposed by ${pending.proposed_by_name}` : ''}
            {pending.proposed_at ? ` at ${formatClinicalDateTime(pending.proposed_at)}` : ''}.
            {pending.you_proposed && distinct ? ' Another signatory must confirm it.' : ''}
          </p>
          {canAmend && (
            <div className="lab-bench-actions">
              {pending.may_confirm && (
                <button type="button" className="lab-bench-btn lab-bench-btn--primary" disabled={busy}
                  onClick={() => void applyProposal()}>
                  {busy ? 'Saving…' : 'Confirm amendment'}
                </button>
              )}
              {mode !== 'cancel' ? (
                <button type="button" className="lab-bench-btn" onClick={() => setMode('cancel')}>
                  Cancel amendment
                </button>
              ) : (
                <>
                  <input className="lab-bench-input" aria-label={`Why the amendment is cancelled, ${name}`}
                    placeholder="Why it is cancelled" value={reason} maxLength={500}
                    onChange={e => setReason(e.target.value)} />
                  <button type="button" className="lab-bench-btn" disabled={busy} onClick={() => {
                    if (!reason.trim()) setError('Say why the amendment is cancelled.')
                    else void cancel()
                  }}>
                    Cancel it
                  </button>
                  <button type="button" className="lab-bench-btn" onClick={reset}>Keep it</button>
                </>
              )}
            </div>
          )}
        </div>
      )}

      {current && canAmend && !pending && mode === 'none' && (
        <div className="lab-bench-actions">
          <button type="button" className="lab-bench-btn" onClick={() => setMode('amend')}>Amend</button>
          <button type="button" className="lab-bench-btn" onClick={() => setMode('withdraw')}>Withdraw</button>
        </div>
      )}

      {current && mode === 'amend' && (
        <div className="lab-bench-section">
          <div className="lab-bench-form">
            <label className="lab-bench-field">
              Amended value
              <input className="lab-bench-input lab-bench-input--value" aria-label={`Amended value, ${name}`}
                value={value} maxLength={2000} onChange={e => setValue(e.target.value)} />
            </label>
            <label className="lab-bench-field">
              Comment
              <input className="lab-bench-input lab-bench-input--comment" aria-label={`Comment, ${name}`}
                value={comment} maxLength={1000} onChange={e => setComment(e.target.value)} />
            </label>
            <label className="lab-bench-field">
              Reason
              <input className="lab-bench-input" aria-label={`Why it is amended, ${name}`}
                value={reason} maxLength={500} onChange={e => setReason(e.target.value)} />
            </label>
            {deltaNote && (
              <label className="lab-bench-field">
                Delta reason
                <input className="lab-bench-input" aria-label={`Delta reason, ${name}`}
                  value={deltaReason} maxLength={500} onChange={e => setDeltaReason(e.target.value)} />
              </label>
            )}
          </div>
          {deltaNote && <p className="lab-bench-quiet">{deltaNote}</p>}
          <div className="lab-bench-actions">
            <button type="button" className="lab-bench-btn lab-bench-btn--primary" disabled={busy}
              onClick={() => {
                if (!value.trim()) setError('Type the amended value.')
                else if (!reason.trim()) setError('Say why the reported result is being amended.')
                else void amend()
              }}>
              {busy ? 'Saving…' : distinct ? 'Propose amendment' : 'Amend result'}
            </button>
            <button type="button" className="lab-bench-btn" onClick={reset}>Keep the reported result</button>
          </div>
          {distinct && (
            <p className="lab-bench-quiet">This site requires a second signatory to confirm an amendment.</p>
          )}
        </div>
      )}

      {current && mode === 'withdraw' && (
        <div className="lab-bench-section">
          <div className="lab-bench-form">
            <label className="lab-bench-field">
              Reason
              <input className="lab-bench-input" aria-label={`Why it is withdrawn, ${name}`}
                value={reason} maxLength={500} onChange={e => setReason(e.target.value)} />
            </label>
          </div>
          <div className="lab-bench-actions">
            <label className="lab-bench-check">
              <input type="checkbox" checked={wrongPatient} onChange={e => setWrongPatient(e.target.checked)}
                aria-label={`This result belonged to another patient, ${name}`} />
              This result belonged to another patient
            </label>
            <button type="button" className="lab-bench-btn lab-bench-btn--primary" disabled={busy}
              onClick={() => {
                if (!reason.trim()) setError('Say why the reported result is withdrawn.')
                else void withdraw()
              }}>
              {busy ? 'Saving…' : 'Withdraw result'}
            </button>
            <button type="button" className="lab-bench-btn" onClick={reset}>Keep the reported result</button>
          </div>
        </div>
      )}

      {error && <p className="lab-bench-error" role="alert">{error}</p>}
    </div>
  )
}

export default function ReportedResults({ onModuleOff }: { onModuleOff: () => void }) {
  const [code, setCode] = useState('')
  const [data, setData] = useState<ReportedResultsData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [warning, setWarning] = useState<string | null>(null)
  const [generation, setGeneration] = useState(0)

  const read = useCallback(async (accession: string) => {
    if (!accession) return
    setLoading(true)
    try {
      setData(await getReportedResults(accession))
      setError(null)
      setGeneration(n => n + 1)
    } catch (err) {
      const failure = toLabError(err)
      if (failure.isModuleDisabled) onModuleOff()
      else {
        setData(null)
        setError(failure.isNotFound ? 'No sample with that number is on record for you.'
          : failure.message || 'The reported results could not be loaded.')
      }
    } finally {
      setLoading(false)
    }
  }, [onModuleOff])

  const find = () => {
    setNotice(null)
    setWarning(null)
    void read(code.replace(/\s+/g, ''))
  }

  const onDone = (message: string) => {
    setWarning(null)
    setNotice(message)
    if (data) void read(data.specimen.id)
  }

  const onFailed = (message: string, stale: boolean) => {
    setNotice(null)
    setWarning(stale ? `${message} The sample has been read again.` : message)
    if (stale && data) void read(data.specimen.id)
  }

  const p = data?.specimen.patient

  return (
    <section className="lab-bench-section" aria-labelledby="lab-reported-title">
      <h3 id="lab-reported-title" className="lab-bench-section-title">Amend or withdraw a reported result</h3>
      <p className="lab-bench-sub">
        A reported result is never changed in place. An amendment makes a new version and keeps the
        old one on record; a withdrawal takes a result entered in error off the chart. The ward is
        told in the patient&apos;s case room.
      </p>
      <div className="lab-bench-form">
        <label className="lab-bench-field">
          Accession number
          <input className="lab-bench-input lab-bench-input--scan" aria-label="Accession number of a reported sample"
            value={code} maxLength={40} onChange={e => setCode(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') find() }} />
        </label>
        <button type="button" className="lab-bench-btn" disabled={loading || !code.trim()} onClick={find}>
          {loading ? 'Finding…' : 'Find sample'}
        </button>
      </div>

      {notice && <p className="lab-bench-note lab-bench-note--ok" role="status">{notice}</p>}
      {warning && <p className="lab-bench-note lab-bench-note--warn" role="status">{warning}</p>}
      {error && <p className="lab-bench-note lab-bench-note--error" role="status">{error}</p>}

      {data && p && (
        <>
          <p className="lab-bench-section-title">
            <span className="lab-bench-num">{formatAccession(data.specimen.id)}</span>
            <span className="lab-bench-quiet">
              {' · '}{p.name}{p.mrn ? ` · UHID ${p.mrn}` : ''}
              {p.discharged ? ' · Patient discharged' : [p.ward, p.bed ? `Bed ${p.bed}` : null].filter(Boolean).map(s => ` · ${s}`).join('')}
              {data.specimen.collected_at ? ` · Collected ${formatClinicalDateTime(data.specimen.collected_at)}` : ''}
            </span>
          </p>
          {data.tests.length === 0 && (
            <p className="lab-bench-empty">Nothing on this sample has been reported yet.</p>
          )}
          {!data.can_amend && data.tests.length > 0 && (
            <p className="lab-bench-quiet">A signatory amends or withdraws a reported result.</p>
          )}
          {data.tests.map(t => (
            <ReportedRow key={`${t.item_id}:${generation}`} test={t} canAmend={data.can_amend}
              distinct={data.require_distinct_authoriser} onDone={onDone} onFailed={onFailed} />
          ))}
        </>
      )}
    </section>
  )
}
