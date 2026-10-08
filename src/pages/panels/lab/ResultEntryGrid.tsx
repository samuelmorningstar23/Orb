/**
 * The result entry grid for one section's worklist (lis-10).
 *
 * Not a view (its name does not end in View.tsx, so the Laboratory panel's
 * glob leaves it alone): WorklistView renders it under the section it picked.
 *
 * ONE ROW PER TEST, grouped under its sample in the order the server sends,
 * which is STAT first, then the oldest receipt. Each row takes the value as
 * typed and says what is wrong before anything is sent, with the same rules
 * the server applies (src/lib/labValue.ts): a number at the test's reporting
 * precision (more places is refused, never rounded), a bound such as <0.01, a
 * value from a coded test's list, or text. The server decides again and
 * decides the flag; the grid shows the server's words for it ("High",
 * "Within range", "No flag: reference range not configured") and never works
 * a flag out itself.
 *
 * A saved row stays editable until the result is technically validated; a
 * change keeps the value before and after on the server, with the reason
 * when one is given.
 *
 * TWO PEOPLE ON ONE BENCH. Every change sends what the row showed of the
 * saved result (value, comment and flag), and the server refuses it
 * (`result_changed`) when someone else has changed the result since, so a
 * stale row never overwrites their correction. If the server refuses a save
 * because the row changed under it (someone else entered, changed or
 * validated it, or the test was cancelled), the grid says so through onStale
 * and the worklist reloads; a row whose result changed on the server is then
 * drawn again from it.
 */
import { Fragment, useState, type KeyboardEvent } from 'react'

import { toLabError } from '../../../api/lab'
import {
  allowedValues,
  editLabResult,
  enterLabResult,
  seenOf,
  type EnteredResult,
  type WorklistItem,
} from '../../../api/lab/results'
import { formatAccession, priorityText } from '../../../api/lab/specimens'
import { formatClinicalTime, parseUTC } from '../../../lib/datetime'
import { matchCodedValue, parseLabNumber } from '../../../lib/labValue'
import './LabBench.css'

export interface ResultEntryGridProps {
  items: WorklistItem[]
  /** Called after a row is saved, with a sentence for the screen. */
  onSaved: (message: string) => void
  /** Called when the server says a row changed under the bench (another
   *  person entered, changed or validated it, the test was cancelled, or its
   *  sample was rejected), with a sentence for the screen. The worklist then
   *  reloads, and a row whose result changed on the server is drawn again
   *  from the server's copy. */
  onStale?: (message: string) => void
  /** Tests only: the clock the turnaround times are read against. */
  now?: Date
}

/** Refusals that mean the row on screen is out of date. */
const STALE_CODES = new Set([
  'result_exists', 'item_cancelled', 'specimen_rejected', 'already_validated', 'not_live',
  'not_received', 'result_changed',
])

/**
 * A row's key carries the server's copy of its result (everything a change
 * says it saw: value, comment and flag), so a reload that brings a different
 * result (someone else's entry or change) draws the row again from it, while
 * a row whose result did not change keeps what is being typed into it.
 */
function rowKey(item: WorklistItem): string {
  const r = item.result
  return `${item.item_id}:${r
    ? `${r.id}:${r.value_text ?? ''}:${r.comment ?? ''}:${r.abnormal_flag ?? ''}`
    : 'none'}`
}

interface SpecimenGroup {
  specimenId: string
  items: WorklistItem[]
}

function groupBySpecimen(items: WorklistItem[]): SpecimenGroup[] {
  const groups: SpecimenGroup[] = []
  for (const item of items) {
    const last = groups[groups.length - 1]
    if (last && last.specimenId === item.specimen_id) last.items.push(item)
    else groups.push({ specimenId: item.specimen_id, items: [item] })
  }
  return groups
}

function minutesText(minutes: number): string {
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours} h ${rest} min` : `${hours} h`
}

function clockText(item: WorklistItem, now: Date): string {
  if (!item.received_at) return ''
  const received = parseUTC(item.received_at)
  if (Number.isNaN(received.getTime())) return ''
  const minutes = Math.max(0, Math.floor((now.getTime() - received.getTime()) / 60000))
  let text = `Received ${formatClinicalTime(item.received_at)}, ${minutesText(minutes)} ago`
  if (item.tat_minutes) {
    const left = item.tat_minutes - minutes
    text += left >= 0
      ? `; due in ${minutesText(left)}`
      : `; over the turnaround time by ${minutesText(-left)}`
  }
  return text
}

function isOutOfRange(result: EnteredResult | null): boolean {
  return !!result && ['H', 'L', 'HH', 'LL', 'A'].includes(result.abnormal_flag)
}

interface RowProps {
  item: WorklistItem
  onSaved: (message: string) => void
  onStale?: (message: string) => void
}

function ResultRow({ item, onSaved, onStale }: RowProps) {
  const { test } = item
  const [saved, setSaved] = useState<EnteredResult | null>(item.result)
  const [value, setValue] = useState(item.result?.value_text ?? '')
  const [comment, setComment] = useState(item.result?.comment ?? '')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const typedProblem = (): string | null => {
    if (test.result_type === 'numeric') {
      const parsed = parseLabNumber(value, test.decimal_places, test.name)
      return parsed.ok ? null : parsed.message
    }
    if (test.result_type === 'coded') {
      return matchCodedValue(value, test.coded_values) ? null : 'Choose one of this test\'s results.'
    }
    return value.trim() ? null : 'Type the result.'
  }

  const changed = saved
    ? value.trim() !== (saved.value_text ?? '') || comment.trim() !== (saved.comment ?? '')
    : value.trim() !== ''

  const save = async () => {
    if (busy || !changed) return
    const problem = typedProblem()
    if (problem) {
      setError(problem)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const out = saved
        ? await editLabResult(saved.id, value.trim(), seenOf(saved), {
          ...(comment.trim() ? { comment: comment.trim() } : {}),
          ...(reason.trim() ? { reason: reason.trim() } : {}),
        })
        : await enterLabResult(item.item_id, value.trim(), comment.trim() || undefined)
      setSaved(out.result)
      setValue(out.result.value_text ?? value.trim())
      setComment(out.result.comment ?? '')
      setReason('')
      onSaved(`${test.name} for ${item.patient.name}: ${out.result.value_text ?? value.trim()} ${saved ? 'changed' : 'saved'}.`)
    } catch (err) {
      const failure = toLabError(err)
      const allowed = failure.code === 'not_in_list' ? allowedValues(failure.detail) : null
      setError(allowed
        ? `${failure.message} The results are: ${allowed.join(', ')}.`
        : failure.message || 'The result could not be saved.')
      if (STALE_CODES.has(failure.code) && onStale) {
        onStale(`${test.name} for ${item.patient.name}: ${failure.message}`)
      }
    } finally {
      setBusy(false)
    }
  }

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      void save()
    }
  }

  const label = `${test.name} for ${item.patient.name}`
  const rangeText = saved?.reference_range_text ?? item.range.text

  return (
    <tr>
      <td>
        <span className="lab-bench-strong">{test.name}</span>
      </td>
      <td>
        {test.result_type === 'coded' ? (
          <select
            className="lab-bench-input lab-bench-input--value"
            aria-label={`Result, ${label}`}
            value={matchCodedValue(value, test.coded_values) ?? ''}
            onChange={e => setValue(e.target.value)}
          >
            <option value="">Choose</option>
            {test.coded_values.map(v => <option key={v} value={v}>{v}</option>)}
          </select>
        ) : (
          <input
            className={`lab-bench-input${test.result_type === 'numeric' ? ' lab-bench-input--value' : ''}`}
            aria-label={`Result, ${label}`}
            value={value}
            inputMode={test.result_type === 'numeric' ? 'decimal' : undefined}
            autoComplete="off"
            spellCheck={test.result_type === 'text'}
            onChange={e => setValue(e.target.value)}
            onKeyDown={onKey}
          />
        )}
        {error && <p className="lab-bench-error" role="alert">{error}</p>}
      </td>
      <td className="lab-bench-quiet">{test.unit}</td>
      <td className="lab-bench-quiet">{test.result_type === 'numeric' ? rangeText : ''}</td>
      <td>
        {saved
          ? <span className={isOutOfRange(saved) ? 'lab-bench-flag lab-bench-flag--out' : 'lab-bench-quiet'}>{saved.flag_text}</span>
          : <span className="lab-bench-quiet">Not entered</span>}
      </td>
      <td>
        <input
          className="lab-bench-input lab-bench-input--comment"
          aria-label={`Comment, ${label}`}
          value={comment}
          maxLength={1000}
          onChange={e => setComment(e.target.value)}
          onKeyDown={onKey}
        />
        {saved && changed && (
          <input
            className="lab-bench-input lab-bench-input--comment"
            aria-label={`Why it is changed, ${label}`}
            placeholder="Why it is changed"
            value={reason}
            maxLength={500}
            onChange={e => setReason(e.target.value)}
            onKeyDown={onKey}
          />
        )}
      </td>
      <td>
        <button
          type="button"
          className="lab-bench-btn lab-bench-btn--primary"
          disabled={busy || !changed}
          onClick={() => void save()}
        >
          {busy ? 'Saving…' : saved ? 'Save change' : 'Save'}
        </button>
      </td>
    </tr>
  )
}

export default function ResultEntryGrid({ items, onSaved, onStale, now }: ResultEntryGridProps) {
  const clock = now ?? new Date()
  const groups = groupBySpecimen(items)
  return (
    <div className="lab-bench-table-wrap">
      <table className="lab-bench-table" aria-label="Result entry">
        <thead>
          <tr>
            <th scope="col">Test</th>
            <th scope="col">Result</th>
            <th scope="col">Unit</th>
            <th scope="col">Reference range</th>
            <th scope="col">Flag</th>
            <th scope="col">Comment</th>
            <th scope="col"><span className="lab-bench-quiet">Save</span></th>
          </tr>
        </thead>
        <tbody>
          {groups.map(group => {
            const first = group.items[0]
            const where = [first.patient.ward, first.patient.bed ? `Bed ${first.patient.bed}` : null]
              .filter(Boolean).join(', ')
            return (
              <Fragment key={group.specimenId}>
                <tr className="lab-bench-group">
                  <td colSpan={7}>
                    {first.priority !== 'routine' && (
                      <span className="lab-bench-stat">{priorityText(first.priority)} </span>
                    )}
                    <span className="lab-bench-num">{formatAccession(group.specimenId)}</span>
                    {` · ${first.patient.name}`}
                    {first.patient.mrn ? ` · UHID ${first.patient.mrn}` : ''}
                    {where ? ` · ${where}` : ''}
                    {clockText(first, clock) ? ` · ${clockText(first, clock)}` : ''}
                  </td>
                </tr>
                {group.items.map(item => (
                  <ResultRow key={rowKey(item)} item={item} onSaved={onSaved} onStale={onStale} />
                ))}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
