/**
 * Orb, the unmatched results queue (lis-19, W109).
 *
 * A result from a connected system whose patient number matched nobody. With
 * ORB_RESULT_QUARANTINE on, it waits here instead of being stored with no
 * patient, where no chart would ever show it.
 *
 * TWO MODES, one list:
 *   reconcile  laboratory staff (ingest.reconcile, ruling LIS-R17). They see
 *              what the message said about the patient and the value, and
 *              attach each result to a patient or discard it with a reason.
 *   monitor    the administrator (ingest.monitor). Counts and a read-only list
 *              of when each arrived, from where, the test and its state. No
 *              patient identifiers, no values, no actions. When the queue is
 *              switched off and empty this mode renders nothing at all.
 *
 * ATTACHING IS A WRONG-PATIENT DECISION. The form asks for the patient's
 * hospital number and the identifiers the person confirmed. The server checks
 * each one against both the message and the patient's record and needs two
 * that match both; it answers per identifier and never shows the record. A
 * refusal is said in words, identifier by identifier, so the person knows
 * whether to recheck the form or call the sending laboratory. Name and date of
 * birth that another record shares confirm nobody, and a result older than a
 * correction already on the chart is refused or placed behind it; each row
 * says whether it is a final, a correction or a preliminary.
 *
 * Flat "Quiet Clinical": no opacity for state, status shown as a border on an
 * ordinary surface with body-token text, tap targets 24px (44px coarse).
 */
import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'

import { type LabApiError } from '../../api/lab'
import {
  attachQuarantined,
  attachRefusal,
  discardQuarantined,
  listQuarantine,
  refusedChecks,
  resultStatusText,
  type AttachRefusal,
  type IdentityCheck,
  type IdentityKind,
  type QuarantineItemFull,
  type QuarantineList,
  type QuarantineStatus,
} from '../../api/lab/quarantine'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../lib/datetime'
import './QuarantineQueue.css'

export interface QuarantineApi {
  list: typeof listQuarantine
  attach: typeof attachQuarantined
  discard: typeof discardQuarantined
}

const REAL_API: QuarantineApi = {
  list: listQuarantine,
  attach: attachQuarantined,
  discard: discardQuarantined,
}

interface QuarantineQueueProps {
  mode: 'reconcile' | 'monitor'
  /** Tests only: the calls to make. */
  api?: QuarantineApi
}

const STATUS_FILTERS: { id: QuarantineStatus; label: string }[] = [
  { id: 'open', label: 'Waiting' },
  { id: 'attached', label: 'Attached' },
  { id: 'discarded', label: 'Discarded' },
  { id: 'all', label: 'All' },
]

const STATE_WORD: Record<string, string> = {
  open: 'Waiting',
  attached: 'Attached',
  discarded: 'Discarded',
}

const KIND_LABEL: Record<IdentityKind, string> = {
  hospital_number: 'Hospital number',
  name: 'Name',
  date_of_birth: 'Date of birth',
}

const CHECK_WORD: Record<IdentityCheck, string> = {
  match: 'Matches the message and the record',
  no_match: 'Does not match',
  not_in_message: 'Not in this message, so it cannot count',
  differs_in_message: 'The message has a different number, so it cannot count',
  not_given: 'Not given',
  names_another_patient: "The message's number now belongs to a different patient's record",
  shared_with_another_patient: "Matches, but another patient's record has the same name and date of birth",
}

/** What a refused attach says, by the server's reason. */
const REFUSAL_WORDS: Record<AttachRefusal, string> = {
  identity_not_confirmed:
    'These identifiers do not confirm the patient. Two must match both this message and the record.',
  message_names_another_patient:
    "A hospital number in this message now belongs to a different patient's record, so it cannot go on this one. Check that record, or ask the sending laboratory which patient it is for.",
  name_and_birth_date_shared:
    "Another patient's record has the same name and date of birth, so these cannot confirm which patient this is. It can be attached only by a hospital number in the message. Otherwise discard it and ask the sending laboratory to resend it with the patient's hospital number.",
  later_correction_on_chart:
    "A corrected result for this test, sent after this one, is already on the patient's chart, so this older result cannot go on top of it. Discard it, or ask the sending laboratory which result stands.",
}

/** The sending laboratory's own flag, in words. Orb computes no flag here. */
const FLAG_WORD: Record<string, string> = {
  H: 'High', L: 'Low', HH: 'Critically high', LL: 'Critically low', A: 'Abnormal', N: 'Normal',
}

type Loaded =
  | { key: string; state: 'ready'; data: QuarantineList }
  | { key: string; state: 'off' }
  | { key: string; state: 'error'; message: string }

type Action = { id: number; kind: 'attach' | 'discard' } | null

interface AttachFields {
  hospital_number: string
  name: string
  date_of_birth: string
  reason: string
}

const EMPTY_ATTACH: AttachFields = { hospital_number: '', name: '', date_of_birth: '', reason: '' }

function flagWord(flag: string | null | undefined): string | null {
  const f = (flag ?? '').trim()
  if (!f) return null
  return FLAG_WORD[f.toUpperCase()] ?? f
}

function asLabError(err: unknown): LabApiError {
  return err as LabApiError
}

export default function QuarantineQueue({ mode, api = REAL_API }: QuarantineQueueProps) {
  const [status, setStatus] = useState<QuarantineStatus>('open')
  const [attempt, setAttempt] = useState(0)
  const [loaded, setLoaded] = useState<Loaded | null>(null)
  const [action, setAction] = useState<Action>(null)
  const [attachFields, setAttachFields] = useState<AttachFields>(EMPTY_ATTACH)
  const [discardReason, setDiscardReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [checks, setChecks] = useState<Partial<Record<IdentityKind, IdentityCheck>> | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const key = `${status}:${attempt}`

  useEffect(() => {
    let live = true
    api.list(status).then(
      (data) => { if (live) setLoaded({ key, state: 'ready', data }) },
      (err: unknown) => {
        if (!live) return
        const failure = asLabError(err)
        if (failure?.code === 'feature_disabled') setLoaded({ key, state: 'off' })
        else setLoaded({ key, state: 'error', message: failure?.message || 'Could not load the list.' })
      },
    )
    return () => { live = false }
  }, [api, status, key])

  const reload = useCallback(() => setAttempt((n) => n + 1), [])

  const closeAction = useCallback(() => {
    setAction(null)
    setAttachFields(EMPTY_ATTACH)
    setDiscardReason('')
    setFormError(null)
    setChecks(null)
  }, [])

  const openAction = useCallback((id: number, kind: 'attach' | 'discard') => {
    setAction({ id, kind })
    setAttachFields(EMPTY_ATTACH)
    setDiscardReason('')
    setFormError(null)
    setChecks(null)
    setNotice(null)
  }, [])

  const submitAttach = useCallback(async (event: FormEvent, item: QuarantineItemFull) => {
    event.preventDefault()
    if (busy) return
    const number = attachFields.hospital_number.trim()
    const reason = attachFields.reason.trim()
    if (!number) { setFormError("Type the patient's hospital number."); return }
    if (!reason) { setFormError('Give a reason.'); return }
    setBusy(true)
    setFormError(null)
    setChecks(null)
    try {
      const done = await api.attach(item.id, {
        hospital_number: number,
        name: attachFields.name.trim() || undefined,
        date_of_birth: attachFields.date_of_birth || undefined,
        reason,
      })
      closeAction()
      const test = item.observation_name ?? 'The result'
      setNotice(done?.superseded_by
        ? `${test} is now on the patient's chart, behind the corrected result the laboratory sent after it.`
        : `${test} is now on the patient's chart.`)
      reload()
    } catch (err) {
      const failure = asLabError(err)
      const refused = refusedChecks(failure?.detail)
      if (refused) {
        setChecks(refused)
        setFormError(REFUSAL_WORDS[attachRefusal(failure?.detail) ?? 'identity_not_confirmed'])
      } else if (failure?.code === 'already_resolved') {
        closeAction()
        setNotice('Someone else decided this result meanwhile.')
        reload()
      } else {
        setFormError(failure?.message || 'Could not attach the result.')
      }
    } finally {
      setBusy(false)
    }
  }, [api, attachFields, busy, closeAction, reload])

  const submitDiscard = useCallback(async (event: FormEvent, item: QuarantineItemFull) => {
    event.preventDefault()
    if (busy) return
    const reason = discardReason.trim()
    if (!reason) { setFormError('Give a reason.'); return }
    setBusy(true)
    setFormError(null)
    try {
      await api.discard(item.id, reason)
      closeAction()
      setNotice(`${item.observation_name ?? 'The result'} was discarded.`)
      reload()
    } catch (err) {
      const failure = asLabError(err)
      if (failure?.code === 'already_resolved') {
        closeAction()
        setNotice('Someone else decided this result meanwhile.')
        reload()
      } else {
        setFormError(failure?.message || 'Could not discard the result.')
      }
    } finally {
      setBusy(false)
    }
  }, [api, busy, closeAction, discardReason, reload])

  // While a reload of the same list is in flight (after a decision), keep the
  // list on screen; a different filter waits for its own answer.
  const current = loaded && (loaded.key === key
    || (loaded.state === 'ready' && loaded.key.split(':')[0] === status)) ? loaded : null
  const data = current?.state === 'ready' ? current.data : null
  const items = useMemo(() => data?.items ?? [], [data])

  // Switched off for new results with nothing in it at all. The server still
  // answers while the laboratory module is on, so the screen says it in words.
  const offAndEmpty = !!data && !data.enabled
    && data.counts.open + data.counts.attached + data.counts.discarded === 0

  // The administrator's screen shows nothing at all for a queue that is off
  // and empty (there is nothing to watch), and nothing until the first answer,
  // so a site that never uses the queue never sees a card flash past.
  if (mode === 'monitor' && (!current || current.state === 'off' || offAndEmpty)) return null

  const intro = mode === 'reconcile'
    ? 'Results from connected systems whose patient number matched nobody. Attach each one to the right patient, or discard it with a reason. Until then no chart shows it.'
    : 'Results from connected systems whose patient number matched nobody. Laboratory staff attach or discard them. This list is read only.'

  return (
    <section className={`rq rq--${mode}`} aria-labelledby={`rq-title-${mode}`}>
      <header className="rq-head">
        <h2 id={`rq-title-${mode}`} className="rq-title">Unmatched results</h2>
        <p className="rq-sub">{intro}</p>
      </header>

      {!current && <p className="rq-status" role="status">Loading unmatched results</p>}

      {current?.state === 'off' && (
        <p className="rq-note" role="status">
          The unmatched results queue is switched off on this site, and nothing is waiting in it.
          A result that matches no patient is stored without one, as before.
        </p>
      )}

      {current?.state === 'error' && (
        <div className="rq-note rq-note--error" role="alert">
          <p className="rq-note__body">{current.message}</p>
          <button type="button" className="rq-btn" onClick={reload}>Try again</button>
        </div>
      )}

      {data && (
        <>
          {!data.enabled && (
            <p className="rq-note">
              {offAndEmpty
                ? 'The unmatched results queue is switched off on this site. A result that matches no patient is stored without one, as before.'
                : 'The queue is switched off for new results. The results already here still need a decision.'}
            </p>
          )}
          {notice && <p className="rq-note rq-note--done" role="status">{notice}</p>}

          <div className="rq-bar">
            <p className="rq-counts" aria-label="Counts">
              <span><strong>{data.counts.open}</strong> waiting</span>
              <span><strong>{data.counts.attached}</strong> attached</span>
              <span><strong>{data.counts.discarded}</strong> discarded</span>
              {data.counts.oldest_open_at && (
                <span>Oldest waiting since {formatClinicalDateTime(data.counts.oldest_open_at)}</span>
              )}
            </p>
            <div className="rq-filters" role="group" aria-label="Show">
              {STATUS_FILTERS.map((f) => (
                <button
                  key={f.id}
                  type="button"
                  className={`rq-filter${status === f.id ? ' rq-filter--on' : ''}`}
                  aria-pressed={status === f.id}
                  onClick={() => { setStatus(f.id); closeAction(); setNotice(null) }}
                >
                  {f.label}
                </button>
              ))}
            </div>
          </div>

          {items.length === 0 && (
            <p className="rq-empty">
              {status === 'open' ? 'Nothing is waiting.' : 'No results in this list.'}
            </p>
          )}

          {mode === 'monitor' && items.length > 0 && (
            <div className="rq-table-wrap">
              <table className="rq-table">
                <thead>
                  <tr>
                    <th scope="col">Received</th>
                    <th scope="col">Test</th>
                    <th scope="col">From</th>
                    <th scope="col">State</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <tr key={item.id}>
                      <td>{formatClinicalDateTime(item.created_at)}</td>
                      <td>{item.observation_name || 'Unnamed test'}</td>
                      <td>{item.sender || 'Not recorded'}</td>
                      <td>{STATE_WORD[item.status] ?? item.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {mode === 'reconcile' && items.length > 0 && (
            <ul className="rq-list">
              {items.map((item) => {
                const flag = flagWord(item.abnormal_flag)
                const resultStatus = resultStatusText(item.result_status)
                const open = item.status === 'open'
                const acting = action?.id === item.id ? action.kind : null
                return (
                  <li key={item.id} className={`rq-item rq-item--${item.status}`}>
                    <div className="rq-item__main">
                      <h3 className="rq-item__title">
                        {item.observation_name || 'Unnamed test'}
                        {item.value != null && item.value !== '' && (
                          <span className="rq-item__value">
                            {' '}{item.value}{item.unit ? ` ${item.unit}` : ''}
                          </span>
                        )}
                      </h3>
                      <p className="rq-item__meta">
                        {resultStatus && <span className="rq-item__result-status">{resultStatus}. </span>}
                        {flag && <span>Laboratory flag: {flag}. </span>}
                        {item.reference_range && <span>Range {item.reference_range}. </span>}
                        <span>Received {formatClinicalDateTime(item.created_at)}</span>
                        {item.sender && <span> from {item.sender}</span>}
                        {item.observed_at && <span>. Taken {formatClinicalDateTime(item.observed_at)}</span>}
                        <span>.</span>
                      </p>
                      <dl className="rq-identity" aria-label="What the message says about the patient">
                        <div>
                          <dt>Number in the message</dt>
                          <dd>{item.message_identifiers?.length ? item.message_identifiers.join(', ') : 'None'}</dd>
                        </div>
                        <div>
                          <dt>Name in the message</dt>
                          <dd>{item.message_name || 'None'}</dd>
                        </div>
                        <div>
                          <dt>Date of birth in the message</dt>
                          <dd>{item.message_date_of_birth ? formatClinicalDateKey(item.message_date_of_birth) : 'None'}</dd>
                        </div>
                        {item.message_sex && (
                          <div>
                            <dt>Sex in the message</dt>
                            <dd>{item.message_sex}</dd>
                          </div>
                        )}
                      </dl>
                      {item.swept && (
                        <p className="rq-item__note">Stored before the queue was switched on.</p>
                      )}
                      {!open && (
                        <p className="rq-item__decision">
                          {STATE_WORD[item.status] ?? item.status}
                          {item.resolved_by_name ? ` by ${item.resolved_by_name}` : ''}
                          {item.resolved_at ? ` on ${formatClinicalDateTime(item.resolved_at)}` : ''}
                          {item.resolution_reason ? `: ${item.resolution_reason}` : ''}
                        </p>
                      )}
                    </div>

                    {open && item.identifiable === false && (
                      <p className="rq-item__note">
                        This message names the patient in only one way, so it cannot be attached:
                        attaching needs two identifiers that match both the message and the record.
                        Discard it and ask the sending laboratory to resend it with the patient's
                        name and date of birth.
                      </p>
                    )}

                    {open && !acting && (
                      <div className="rq-item__actions">
                        {item.identifiable !== false && (
                          <button type="button" className="rq-btn rq-btn--primary"
                            onClick={() => openAction(item.id, 'attach')}>
                            Attach to a patient
                          </button>
                        )}
                        <button type="button" className="rq-btn"
                          onClick={() => openAction(item.id, 'discard')}>
                          Discard
                        </button>
                      </div>
                    )}

                    {open && acting === 'attach' && (
                      <form className="rq-form" onSubmit={(e) => submitAttach(e, item)}
                        aria-label={`Attach ${item.observation_name || 'result'} to a patient`}>
                        <p className="rq-form__help">
                          Type at least two identifiers you have confirmed on the request form or the
                          patient's wristband. Each one must match both this message and the patient's
                          record.
                        </p>
                        <label className="rq-field">
                          <span>Hospital number</span>
                          <input value={attachFields.hospital_number} autoComplete="off" required
                            onChange={(e) => setAttachFields((f) => ({ ...f, hospital_number: e.target.value }))} />
                        </label>
                        <label className="rq-field">
                          <span>Full name</span>
                          <input value={attachFields.name} autoComplete="off"
                            onChange={(e) => setAttachFields((f) => ({ ...f, name: e.target.value }))} />
                        </label>
                        <label className="rq-field">
                          <span>Date of birth</span>
                          <input type="date" value={attachFields.date_of_birth}
                            onChange={(e) => setAttachFields((f) => ({ ...f, date_of_birth: e.target.value }))} />
                        </label>
                        <label className="rq-field rq-field--wide">
                          <span>Reason</span>
                          <textarea value={attachFields.reason} rows={2} maxLength={500} required
                            onChange={(e) => setAttachFields((f) => ({ ...f, reason: e.target.value }))} />
                        </label>
                        {formError && <p className="rq-form__error" role="alert">{formError}</p>}
                        {checks && (
                          <ul className="rq-checks" aria-label="Identifier checks">
                            {(Object.keys(KIND_LABEL) as IdentityKind[]).filter((k) => checks[k]).map((k) => (
                              <li key={k} className={`rq-check rq-check--${checks[k]}`}>
                                <span className="rq-check__kind">{KIND_LABEL[k]}</span>
                                <span>{CHECK_WORD[checks[k] as IdentityCheck] ?? checks[k]}</span>
                              </li>
                            ))}
                          </ul>
                        )}
                        <div className="rq-form__actions">
                          <button type="submit" className="rq-btn rq-btn--primary" disabled={busy}>
                            {busy ? 'Attaching' : 'Attach result'}
                          </button>
                          <button type="button" className="rq-btn" onClick={closeAction} disabled={busy}>
                            Cancel
                          </button>
                        </div>
                      </form>
                    )}

                    {open && acting === 'discard' && (
                      <form className="rq-form" onSubmit={(e) => submitDiscard(e, item)}
                        aria-label={`Discard ${item.observation_name || 'result'}`}>
                        <p className="rq-form__help">
                          A discarded result never reaches a chart. Say why, for example that the sending
                          laboratory has been asked to resend it with the right number.
                        </p>
                        <label className="rq-field rq-field--wide">
                          <span>Reason</span>
                          <textarea value={discardReason} rows={2} maxLength={500} required
                            onChange={(e) => setDiscardReason(e.target.value)} />
                        </label>
                        {formError && <p className="rq-form__error" role="alert">{formError}</p>}
                        <div className="rq-form__actions">
                          <button type="submit" className="rq-btn rq-btn--danger" disabled={busy}>
                            {busy ? 'Discarding' : 'Discard result'}
                          </button>
                          <button type="button" className="rq-btn" onClick={closeAction} disabled={busy}>
                            Cancel
                          </button>
                        </div>
                      </form>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </>
      )}
    </section>
  )
}
