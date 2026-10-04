/**
 * Reference ranges for one numeric laboratory test (lis-4), and the shared
 * editor the critical limits use too (./CriticalLimitEditor.tsx).
 *
 * NOT CONFIGURED IS SAID, NEVER IMPLIED. Orb ships no reference range and no
 * critical limit (rulings LIS-R1 and LIS-R2). A test with none says what that
 * means for a result, in words, above everything else here.
 *
 * A RANGE IS NEVER EDITED. Approving a range for the same sex and age band
 * replaces the current one from now on (the server closes the old row and
 * inserts the new one), and retiring one needs a reason. Every approval needs
 * an approval note (the method or source the values come from) and the
 * approver's own confirmation; a critical limit also names the clinicians it
 * was agreed with. The approver is the signed-in pathologist, recorded by the
 * server.
 *
 * Values are sent as the strings typed, and checked here against the test's
 * reporting precision before sending; the server checks them again and its
 * answer (precision, overlap, permission) is shown in its own words.
 */
import { useEffect, useId, useState, type FormEvent } from 'react'

import { toLabError } from '../../../api/lab'
import {
  approveLabCriticalLimit,
  approveLabReferenceRange,
  listLabCriticalLimits,
  listLabReferenceRanges,
  retireLabCriticalLimit,
  retireLabReferenceRange,
  type LabCriticalLimit,
  type LabReferenceRange,
  type LabSex,
  type LabTest,
} from '../../../api/lab/master'
import { formatClinicalDate, formatClinicalDateTime } from '../../../lib/datetime'
import { aboutAge, bandText, parseDays, precisionProblem } from './labSetupModel'

type Band = LabReferenceRange | LabCriticalLimit

export interface BandEditorProps {
  kind: 'range' | 'limit'
  test: Pick<LabTest, 'id' | 'name' | 'unit' | 'decimal_places' | 'active'>
  /** The current approved rows for this test, as GET /master/tests/{id} gave them. */
  current: Band[]
  /** Holds lab.master and the module is on. */
  canEdit: boolean
  /** Called after an approval or a retirement, so the caller reloads. */
  onChanged: () => void
}

const WORDS = {
  range: {
    title: 'Reference ranges',
    one: 'range',
    none: 'Reference range not configured. Results for this test will carry no high or low flag until a range is approved.',
    approve: 'Approve a reference range',
    approveButton: 'Approve range',
    low: 'Lower value',
    high: 'Upper value',
    confirm: 'I approve this reference range for use on patient results.',
  },
  limit: {
    title: 'Critical limits',
    one: 'limit',
    none: 'Critical limit not configured. No critical result will be raised for this test until a limit is approved.',
    approve: 'Approve a critical limit',
    approveButton: 'Approve limit',
    low: 'Critical below',
    high: 'Critical above',
    confirm: 'I approve this critical limit for use on patient results.',
  },
} as const

function isLimit(band: Band): band is LabCriticalLimit {
  return 'agreed_with' in band
}

function BandRow({ band, kind, canEdit, onRetired }: {
  band: Band
  kind: 'range' | 'limit'
  canEdit: boolean
  onRetired: () => void
}) {
  const [retiring, setRetiring] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const reasonId = useId()
  const retired = band.effective_to !== null

  async function retire(event: FormEvent) {
    event.preventDefault()
    if (!reason.trim()) {
      setError('Write why this one is being retired.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      if (kind === 'range') await retireLabReferenceRange(band.id, reason.trim())
      else await retireLabCriticalLimit(band.id, reason.trim())
      setRetiring(false)
      setReason('')
      onRetired()
    } catch (err) {
      setError(toLabError(err).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <li className={`lab-setup-band${retired ? ' lab-setup-band--retired' : ''}`}>
      <div className="lab-setup-band__main">
        <span className="lab-setup-band__value">{band.display_text}</span>
        <span className="lab-setup-band__who">
          {bandText(band.sex, band.age_min_days, band.age_max_days)}
        </span>
      </div>
      <dl className="lab-setup-band__facts">
        <div>
          <dt>Approved</dt>
          <dd>{band.approved_by}, {formatClinicalDateTime(band.approved_at)}</dd>
        </div>
        {isLimit(band) && (
          <div>
            <dt>Agreed with</dt>
            <dd>{band.agreed_with}</dd>
          </div>
        )}
        <div>
          <dt>Approval note</dt>
          <dd>{band.method_note}</dd>
        </div>
        {retired && (
          <div>
            <dt>Retired</dt>
            <dd>{formatClinicalDate(band.effective_to)}</dd>
          </div>
        )}
      </dl>
      {canEdit && !retired && !retiring && (
        <button type="button" className="lab-setup-btn" onClick={() => setRetiring(true)}>
          Retire
        </button>
      )}
      {retiring && (
        <form className="lab-setup-inline" onSubmit={retire}>
          <label htmlFor={reasonId}>Why is this {WORDS[kind].one} being retired?</label>
          <input
            id={reasonId}
            className="lab-setup-input"
            value={reason}
            maxLength={500}
            onChange={(e) => setReason(e.target.value)}
          />
          <div className="lab-setup-actions">
            <button type="submit" className="lab-setup-btn lab-setup-btn--primary" disabled={busy}>
              {busy ? 'Retiring' : `Retire ${WORDS[kind].one}`}
            </button>
            <button type="button" className="lab-setup-btn" onClick={() => {
              setRetiring(false)
              setError(null)
            }}>
              Keep it
            </button>
          </div>
          {error && <p className="lab-setup-error" role="alert">{error}</p>}
        </form>
      )}
    </li>
  )
}

function ApproveForm({ kind, test, onApproved }: {
  kind: 'range' | 'limit'
  test: BandEditorProps['test']
  onApproved: () => void
}) {
  const words = WORDS[kind]
  const [sex, setSex] = useState<LabSex>('any')
  const [ageMin, setAgeMin] = useState('0')
  const [ageMax, setAgeMax] = useState('')
  const [low, setLow] = useState('')
  const [high, setHigh] = useState('')
  const [note, setNote] = useState('')
  const [agreedWith, setAgreedWith] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const id = useId()
  const dp = test.decimal_places
  const unit = test.unit ? ` (${test.unit})` : ''

  function problem(): string | null {
    const min = parseDays(ageMin)
    const max = parseDays(ageMax)
    if (typeof min === 'string') return min
    if (typeof max === 'string') return max
    if (max !== null && max <= (min ?? 0)) return 'The upper age must be above the lower age.'
    if (!low.trim() && !high.trim()) return 'Give a lower value, an upper value, or both.'
    const lowProblem = precisionProblem(low, dp)
    if (lowProblem) return `${words.low}: ${lowProblem}`
    const highProblem = precisionProblem(high, dp)
    if (highProblem) return `${words.high}: ${highProblem}`
    if (low.trim() && high.trim()) {
      const a = Number(low)
      const b = Number(high)
      if (kind === 'limit' ? b <= a : b < a) return 'The upper value must be above the lower value.'
    }
    if (!note.trim()) return 'Write the approval note: the method, analyser or source these values come from.'
    if (kind === 'limit' && !agreedWith.trim()) {
      return 'Name the clinicians this limit was agreed with.'
    }
    if (!confirmed) return 'Tick the box to confirm you are approving it.'
    return null
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    setDone(null)
    const wrong = problem()
    if (wrong) {
      setError(wrong)
      return
    }
    setBusy(true)
    setError(null)
    const minDays = parseDays(ageMin)
    const maxDays = parseDays(ageMax)
    const band = {
      test_id: test.id,
      sex,
      age_min_days: typeof minDays === 'number' ? minDays : 0,
      age_max_days: typeof maxDays === 'number' ? maxDays : null,
    }
    try {
      const saved = kind === 'range'
        ? await approveLabReferenceRange({
          ...band, low: low.trim() || null, high: high.trim() || null, method_note: note.trim(),
        })
        : await approveLabCriticalLimit({
          ...band,
          critical_low: low.trim() || null,
          critical_high: high.trim() || null,
          agreed_with: agreedWith.trim(),
          method_note: note.trim(),
        })
      setDone(`Approved: ${saved.display_text}`)
      setLow('')
      setHigh('')
      setNote('')
      setAgreedWith('')
      setConfirmed(false)
      onApproved()
    } catch (err) {
      setError(toLabError(err).message)
    } finally {
      setBusy(false)
    }
  }

  const minDays = parseDays(ageMin)
  const maxDays = parseDays(ageMax)

  return (
    <form className="lab-setup-form" onSubmit={submit} aria-labelledby={`${id}-title`}>
      <h4 id={`${id}-title`} className="lab-setup-form__title">{words.approve}</h4>
      <p className="lab-setup-hint">
        Ages are in days at collection, from the date of birth. A patient with no date of
        birth only gets a {words.one} that covers every age.
        {dp === 0 ? ' This test reports whole numbers.' : ` This test reports ${dp} decimal place${dp === 1 ? '' : 's'}.`}
      </p>
      <div className="lab-setup-grid">
        <label className="lab-setup-field">
          <span>Sex</span>
          <select className="lab-setup-input" value={sex}
            onChange={(e) => setSex(e.target.value as LabSex)}>
            <option value="any">Any sex</option>
            <option value="male">Male</option>
            <option value="female">Female</option>
          </select>
        </label>
        <label className="lab-setup-field">
          <span>From age in days</span>
          <input className="lab-setup-input" inputMode="numeric" value={ageMin}
            onChange={(e) => setAgeMin(e.target.value)} />
          {typeof minDays === 'number' && minDays > 0 && (
            <small className="lab-setup-hint">{aboutAge(minDays)}</small>
          )}
        </label>
        <label className="lab-setup-field">
          <span>Under age in days (blank for no upper age)</span>
          <input className="lab-setup-input" inputMode="numeric" value={ageMax}
            onChange={(e) => setAgeMax(e.target.value)} />
          {typeof maxDays === 'number' && (
            <small className="lab-setup-hint">{aboutAge(maxDays)}</small>
          )}
        </label>
        <label className="lab-setup-field">
          <span>{words.low}{unit}</span>
          <input className="lab-setup-input" inputMode="decimal" value={low}
            onChange={(e) => setLow(e.target.value)} />
        </label>
        <label className="lab-setup-field">
          <span>{words.high}{unit}</span>
          <input className="lab-setup-input" inputMode="decimal" value={high}
            onChange={(e) => setHigh(e.target.value)} />
        </label>
      </div>
      <label className="lab-setup-field lab-setup-field--wide">
        <span>Approval note (method, analyser or source)</span>
        <textarea className="lab-setup-input lab-setup-textarea" value={note} maxLength={500}
          onChange={(e) => setNote(e.target.value)} />
      </label>
      {kind === 'limit' && (
        <label className="lab-setup-field lab-setup-field--wide">
          <span>Agreed with (the clinicians who agreed this limit)</span>
          <input className="lab-setup-input" value={agreedWith} maxLength={500}
            onChange={(e) => setAgreedWith(e.target.value)} />
        </label>
      )}
      <label className="lab-setup-check">
        <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
        <span>{words.confirm}</span>
      </label>
      <div className="lab-setup-actions">
        <button type="submit" className="lab-setup-btn lab-setup-btn--primary" disabled={busy}>
          {busy ? 'Approving' : words.approveButton}
        </button>
      </div>
      {error && <p className="lab-setup-error" role="alert">{error}</p>}
      {done && <p className="lab-setup-done" role="status">{done}</p>}
    </form>
  )
}

/** The current (and, on request, retired) rows of one kind for one test,
 *  with retire and approve for a pathologist. */
export function BandEditor({ kind, test, current, canEdit, onChanged }: BandEditorProps) {
  const words = WORDS[kind]
  const [showRetired, setShowRetired] = useState(false)
  const [retired, setRetired] = useState<{ key: string; rows: Band[] } | null>(null)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const historyKey = `${test.id}:${current.map((b) => b.id).join(',')}`

  useEffect(() => {
    if (!showRetired) return
    let live = true
    const request = kind === 'range'
      ? listLabReferenceRanges({ test_id: test.id, include_retired: true })
        .then((r) => r.ranges as Band[])
      : listLabCriticalLimits({ test_id: test.id, include_retired: true })
        .then((r) => r.critical_limits as Band[])
    request.then(
      (rows) => {
        if (!live) return
        setHistoryError(null)
        setRetired({ key: historyKey, rows: rows.filter((b) => b.effective_to !== null) })
      },
      (err: unknown) => {
        if (live) setHistoryError(toLabError(err).message)
      },
    )
    return () => { live = false }
  }, [showRetired, kind, test.id, historyKey])

  const retiredRows = retired && retired.key === historyKey ? retired.rows : null
  const editable = canEdit && Boolean(test.active)

  return (
    <section className="lab-setup-bands" aria-label={words.title}>
      <h3 className="lab-setup-bands__title">{words.title}</h3>
      {current.length === 0 && (
        <p className="lab-setup-gap">{words.none}</p>
      )}
      {current.length > 0 && (
        <ul className="lab-setup-band-list">
          {current.map((band) => (
            <BandRow key={band.id} band={band} kind={kind} canEdit={editable}
              onRetired={onChanged} />
          ))}
        </ul>
      )}
      <label className="lab-setup-check">
        <input type="checkbox" checked={showRetired}
          onChange={(e) => setShowRetired(e.target.checked)} />
        <span>Show retired {words.one}s</span>
      </label>
      {showRetired && historyError && <p className="lab-setup-error" role="alert">{historyError}</p>}
      {showRetired && !historyError && retiredRows === null && (
        <p className="lab-setup-hint" role="status">Loading retired {words.one}s</p>
      )}
      {showRetired && retiredRows !== null && retiredRows.length === 0 && (
        <p className="lab-setup-hint">No retired {words.one}s.</p>
      )}
      {showRetired && retiredRows !== null && retiredRows.length > 0 && (
        <ul className="lab-setup-band-list">
          {retiredRows.map((band) => (
            <BandRow key={band.id} band={band} kind={kind} canEdit={false} onRetired={onChanged} />
          ))}
        </ul>
      )}
      {canEdit && !test.active && (
        <p className="lab-setup-hint">This test is switched off, so nothing can be approved for it.</p>
      )}
      {editable && <ApproveForm kind={kind} test={test} onApproved={onChanged} />}
      {!canEdit && (
        <p className="lab-setup-hint">Only a pathologist can approve or retire a {words.one}.</p>
      )}
    </section>
  )
}

export default function RangeEditor(props: Omit<BandEditorProps, 'kind' | 'current'> & {
  ranges: LabReferenceRange[]
}) {
  const { ranges, ...rest } = props
  return <BandEditor kind="range" current={ranges} {...rest} />
}
